"""prompt_agent runtime: OpenAI Realtime OWNS the whole patient conversation.

Unlike controlled/native mode, the backend here authors NOTHING. Realtime
listens, decides turn-taking (server_vad), and answers the student naturally in
the patient's voice using a hosted per-patient prompt. This runtime's ONLY jobs
are the transcript/persistence side-effects the rest of the app already depends
on:

  - stream Realtime's patient audio out to the LiveKit AudioSource (via the
    worker's on_audio),
  - persist the FINAL student utterance as a normal student ConversationTurn and
    publish a `student_transcript` transcript_sync event carrying its DB id,
  - persist the FINAL patient utterance as a normal patient ConversationTurn and
    publish a `patient_text_final` transcript_sync event carrying its DB id.

It bypasses RealtimeTurnController, patient_adapter generation, native staging,
Deepgram/Silero/Smart Turn, ElevenLabs and the conversation:"none" speak() path
entirely - Realtime is the conversational brain in this mode.

Bound to exactly one RealtimeSession (one interview), same isolation contract as
the other runtimes: a failure in here is caught/logged and never breaks the
student's audio ingest or the Realtime receive loop.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable

from app.core.constants import PROMPT_VERSION, ROLE_PATIENT
from app.core.logging import get_logger
from app.livekit_agent import native_agent
from app.repositories.transcript_repository import TranscriptRepository

logger = get_logger("app.livekit_agent.realtime")

# GA Realtime server-event types this runtime acts on.
_EVT_SPEECH_STARTED = "input_audio_buffer.speech_started"
_EVT_INPUT_TRANSCRIPTION_DONE = "conversation.item.input_audio_transcription.completed"
_EVT_RESPONSE_CREATED = "response.created"
_EVT_RESPONSE_AUDIO_DELTA = "response.output_audio.delta"
_EVT_RESPONSE_TRANSCRIPT_DELTA = "response.output_audio_transcript.delta"
_EVT_RESPONSE_TRANSCRIPT_DONE = "response.output_audio_transcript.done"
_EVT_RESPONSE_DONE = "response.done"

# on_audio(pcm24_bytes) - awaited per patient-audio delta so the worker can
# stream frames straight into its LiveKit AudioSource.
AudioSink = Callable[[bytes], Awaitable[None]]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class PromptAgentRuntime:
    """Event-driven transcript/persistence side-car for prompt_agent mode."""

    def __init__(
        self,
        *,
        session_id: str,
        case_id: str,
        config: dict[str, Any],
        db_factory: Callable[[], Any],
        on_audio: AudioSink,
        on_student_final: Callable[[str, int, str, str], None],
        on_patient_final: Callable[[str, int, str, str], None],
    ) -> None:
        self._session_id = session_id
        self._case_id = case_id
        self._config = config
        self._db_factory = db_factory
        self._on_audio = on_audio
        self._on_student_final = on_student_final
        self._on_patient_final = on_patient_final
        self._session: Any | None = None

        # Monotonic transcript-sync epoch. The frontend drops any event whose
        # epoch is lower than the highest it has seen, so student-then-patient
        # ordering is preserved by simply incrementing on every publish.
        self._epoch = 0
        # The in-flight Realtime response id (from response.created). Patient
        # audio/transcript events are correlated to it.
        self._active_response_id: str | None = None
        # Accumulated patient transcript deltas, keyed by response id.
        self._transcript_parts: dict[str, list[str]] = {}
        self._transcript_final: dict[str, str] = {}
        # Response ids already persisted, so a duplicate terminal event is a
        # no-op rather than a second DB row.
        self._finalized_responses: set[str] = set()
        # Response ids the student barged in on: their late audio deltas are
        # dropped so a cancelled answer never keeps playing.
        self._interrupted_responses: set[str] = set()

    # ---- readiness / configuration surface (mirrors native runtime) --------
    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def bind_session(self, session: Any) -> None:
        self._session = session

    def _next_epoch(self) -> int:
        self._epoch += 1
        return self._epoch

    async def handle_event(self, event_type: str, event: Any) -> None:
        """Single entry point, awaited by RealtimeSession's receive loop for
        every server event. Fully guarded - never raises into that loop."""
        try:
            if event_type == _EVT_RESPONSE_CREATED:
                self._on_response_created(event)
            elif event_type == _EVT_SPEECH_STARTED:
                self._on_speech_started()
            elif event_type == _EVT_RESPONSE_AUDIO_DELTA:
                await self._on_audio_delta(event)
            elif event_type == _EVT_INPUT_TRANSCRIPTION_DONE:
                await self._on_student_transcription_done(event)
            elif event_type == _EVT_RESPONSE_TRANSCRIPT_DELTA:
                self._on_patient_transcript_delta(event)
            elif event_type == _EVT_RESPONSE_TRANSCRIPT_DONE:
                self._on_patient_transcript_done(event)
            elif event_type == _EVT_RESPONSE_DONE:
                await self._on_response_done(event)
        except Exception:
            logger.exception(
                "prompt_agent_event_failed session_id=%s event=%s",
                self._session_id, event_type,
            )

    # ---- Realtime response lifecycle --------------------------------------
    def _on_response_created(self, event: Any) -> None:
        resp = _get(event, "response")
        response_id = _get(resp, "id") if resp is not None else _get(event, "response_id")
        self._active_response_id = str(response_id) if response_id else None

    def _on_speech_started(self) -> None:
        # Barge-in: interrupt_response=True makes OpenAI cancel its own answer,
        # but late audio deltas already in flight for that response must not
        # keep playing. Mark it so _on_audio_delta drops them; the worker
        # separately clears the LiveKit playback buffer (on_speech_started).
        if self._active_response_id:
            self._interrupted_responses.add(self._active_response_id)

    async def _on_audio_delta(self, event: Any) -> None:
        response_id = str(_get(event, "response_id") or self._active_response_id or "")
        if response_id and response_id in self._interrupted_responses:
            return  # stale audio from an interrupted/cancelled response
        delta = _get(event, "delta")
        if not delta:
            return
        try:
            pcm = base64.b64decode(delta)
        except Exception:
            logger.exception(
                "prompt_agent_audio_decode_failed session_id=%s", self._session_id,
            )
            return
        if pcm:
            await self._on_audio(pcm)

    def _on_patient_transcript_delta(self, event: Any) -> None:
        response_id = str(_get(event, "response_id") or self._active_response_id or "")
        if not response_id:
            return
        self._transcript_parts.setdefault(response_id, []).append(_get(event, "delta") or "")

    def _on_patient_transcript_done(self, event: Any) -> None:
        response_id = str(_get(event, "response_id") or self._active_response_id or "")
        if response_id:
            self._transcript_final[response_id] = _get(event, "transcript") or ""

    async def _on_response_done(self, event: Any) -> None:
        resp = _get(event, "response")
        response_id = str(
            (_get(resp, "id") if resp is not None else None)
            or _get(event, "response_id")
            or self._active_response_id
            or ""
        )
        if not response_id or response_id in self._finalized_responses:
            return
        self._finalized_responses.add(response_id)
        final = self._transcript_final.pop(response_id, None)
        if final is None:
            final = "".join(self._transcript_parts.pop(response_id, []))
        else:
            self._transcript_parts.pop(response_id, None)
        self._interrupted_responses.discard(response_id)
        text = (final or "").strip()
        if not text:
            return
        client_turn_id = f"rt-{response_id}"
        patient_client_turn_id = f"{client_turn_id}:patient"
        try:
            patient_turn_id = await asyncio.get_running_loop().run_in_executor(
                None, self._persist_patient_sync, patient_client_turn_id, text,
            )
        except Exception:
            logger.exception(
                "prompt_agent_patient_persist_failed session_id=%s response_id=%s",
                self._session_id, response_id,
            )
            return
        self._on_patient_final(client_turn_id, self._next_epoch(), patient_turn_id, text)

    # ---- student final transcript -----------------------------------------
    async def _on_student_transcription_done(self, event: Any) -> None:
        text = (_get(event, "transcript") or "").strip()
        if not text:
            return
        item_id = str(_get(event, "item_id") or "")
        client_turn_id = f"rt-{item_id}" if item_id else f"rt-student-{self._epoch + 1}"
        try:
            student_turn_id = await asyncio.get_running_loop().run_in_executor(
                None, self._persist_student_sync, client_turn_id, text,
            )
        except Exception:
            logger.exception(
                "prompt_agent_student_persist_failed session_id=%s item_id=%s",
                self._session_id, item_id,
            )
            return
        self._on_student_final(client_turn_id, self._next_epoch(), student_turn_id, text)

    # ---- blocking DB persistence (run in executor) ------------------------
    def _persist_student_sync(self, client_turn_id: str, text: str) -> str:
        db = self._db_factory()
        try:
            # Reuse the existing, idempotent student-turn writer (dedups by
            # client_turn_id and validates session ownership/lock state).
            saved = native_agent.persist_student_turn_once(
                db,
                session_id=self._session_id,
                case_id=self._case_id,
                client_turn_id=client_turn_id,
                text=text,
                source="openai_realtime",
            )
            return saved.id
        finally:
            db.close()

    def _persist_patient_sync(self, client_turn_id: str, text: str) -> str:
        db = self._db_factory()
        try:
            repo = TranscriptRepository(db)
            existing = repo.get_by_client_turn_id(self._session_id, client_turn_id)
            if existing is not None:
                return existing.id
            turn = repo.append_turn(
                self._session_id,
                ROLE_PATIENT,
                text.strip(),
                client_turn_id=client_turn_id,
                source="openai_realtime",
                model_name=self._config.get("model"),
                prompt_version=PROMPT_VERSION,
                response_type="answer",
                validation_status="valid",
                speaker_id="patient",
                speaker_label="",
            )
            db.commit()
            return turn.id
        finally:
            db.close()
