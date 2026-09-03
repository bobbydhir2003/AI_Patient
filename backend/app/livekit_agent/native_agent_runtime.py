"""Small event/correlation layer for the tool-authorized native Realtime mode.

This is intentionally not another turn detector: OpenAI owns VAD, turn end,
conversation state, and response generation.  The runtime only correlates
provider IDs to durable turns, executes the two restricted tools, streams PCM,
and linearizes final persistence with generation authority.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger
from app.livekit_agent import native_agent
from app.livekit_agent.realtime_client import (
    NATIVE_ALLOWED_FACTS_TOOL,
    NATIVE_STAGE_RESPONSE_TOOL,
)

logger = get_logger("app.livekit_agent.native")
_TOOL_TIMEOUT_SECONDS = 12.0
_MAX_FINISHED = 256


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass
class _Turn:
    item_id: str
    client_turn_id: str
    source: str = "speech"
    text: str = ""
    epoch: int | None = None
    student_turn_id: str | None = None
    transcript_ready: asyncio.Event = field(default_factory=asyncio.Event)
    authorization: native_agent.FactAuthorization | None = None
    staged: native_agent.StagedPatientResponse | None = None
    response_phases: dict[str, str] = field(default_factory=dict)
    transcript_parts: list[str] = field(default_factory=list)
    final_transcript: str = ""
    first_audio_at: float | None = None
    audio_bytes: int = 0
    persisted_patient_id: str | None = None
    finished: bool = False


class NativeRealtimeAgentRuntime:
    def __init__(
        self,
        *,
        session_id: str,
        case_id: str,
        model_name: str,
        db_factory: Callable[[], Any],
        reserve_generation: Callable[[str], int],
        generation_is_current: Callable[[int], bool],
        generation_authority: Any,
        on_audio: Callable[[bytes], Awaitable[None]],
        on_speaking_started: Callable[[str, str], None],
        on_patient_final: Callable[[str, int, native_agent.PersistedNativePatientTurn, str], None],
        on_student_persisted: Callable[[str, int, str], None],
        on_status: Callable[[str, str], None],
    ) -> None:
        self.session_id = session_id
        self.case_id = case_id
        self.model_name = model_name
        self._db_factory = db_factory
        self._reserve_generation = reserve_generation
        self._generation_is_current = generation_is_current
        self._generation_authority = generation_authority
        self._on_audio = on_audio
        self._on_speaking_started = on_speaking_started
        self._on_patient_final = on_patient_final
        self._on_student_persisted = on_student_persisted
        self._on_status = on_status
        self._session: Any = None
        self._turns: "OrderedDict[str, _Turn]" = OrderedDict()
        self._response_turns: dict[str, _Turn] = {}
        self._call_names: dict[str, str] = {}
        self._call_responses: dict[str, str] = {}
        self._handled_calls: set[str] = set()

    @property
    def instructions(self) -> str:
        return native_agent.build_patient_instructions()

    def bind_session(self, session: Any) -> None:
        self._session = session

    def input_committed(self, item_id: str) -> None:
        if not item_id or item_id in self._turns:
            return
        self._turns[item_id] = _Turn(
            item_id=item_id,
            client_turn_id=native_agent.stable_native_client_turn_id(self.session_id, item_id),
        )
        self._trim()

    async def submit_typed_text(self, text: str, client_turn_id: str) -> None:
        item_id = f"typed-{client_turn_id}"[:64]
        turn = self._turns.get(item_id)
        if turn is None:
            turn = _Turn(item_id=item_id, client_turn_id=client_turn_id, source="typed")
            self._turns[item_id] = turn
        await self._accept_transcript(turn, text)
        await self._session.send_event({
            "type": "conversation.item.create",
            "item": {
                "id": item_id,
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        await self._session.send_event(self._authorize_response_event(turn))

    async def handle_event(self, event_type: str, event: Any) -> None:
        if event_type == "input_audio_buffer.committed":
            self.input_committed(str(_get(event, "item_id") or ""))
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            item_id = str(_get(event, "item_id") or "")
            self.input_committed(item_id)
            turn = self._turns.get(item_id)
            if turn is not None:
                await self._accept_transcript(turn, str(_get(event, "transcript") or ""))
            return
        if event_type == "response.created":
            self._response_created(event)
            return
        if event_type in ("response.output_item.added", "response.output_item.done"):
            item = _get(event, "item")
            if _get(item, "type") == "function_call":
                call_id = str(_get(item, "call_id") or "")
                if call_id:
                    self._call_names[call_id] = str(_get(item, "name") or "")
                    self._call_responses[call_id] = str(_get(event, "response_id") or "")
            return
        if event_type == "response.function_call_arguments.done":
            await self._handle_tool_call(event)
            return
        if event_type == "response.output_audio.delta":
            await self._handle_audio(event)
            return
        if event_type == "response.output_audio_transcript.delta":
            turn = self._response_turn(event)
            if turn is not None and self._phase(turn, event) == "speak":
                turn.transcript_parts.append(str(_get(event, "delta") or ""))
            return
        if event_type == "response.output_audio_transcript.done":
            turn = self._response_turn(event)
            if turn is not None and self._phase(turn, event) == "speak":
                turn.final_transcript = str(_get(event, "transcript") or "").strip()
            return
        if event_type == "response.done":
            await self._response_done(event)

    async def _accept_transcript(self, turn: _Turn, text: str) -> None:
        text = text.strip()
        if not text or turn.transcript_ready.is_set():
            return
        turn.text = text
        # This synchronous reservation is the authoritative turn boundary;
        # provider task scheduling never owns generation order.
        turn.epoch = self._reserve_generation(turn.client_turn_id)
        loop = asyncio.get_running_loop()
        turn.student_turn_id = await loop.run_in_executor(None, self._persist_student, turn)
        turn.transcript_ready.set()
        self._on_student_persisted(turn.client_turn_id, turn.epoch, text)
        logger.info(
            "native_student_transcription_final session_id=%s client_turn_id=%s item_id=%s",
            self.session_id, turn.client_turn_id, turn.item_id,
        )

    def _persist_student(self, turn: _Turn) -> str:
        db = self._db_factory()
        try:
            saved = native_agent.persist_student_turn_once(
                db,
                session_id=self.session_id,
                case_id=self.case_id,
                client_turn_id=turn.client_turn_id,
                text=turn.text,
                source=turn.source,
            )
            return saved.id
        finally:
            db.close()

    def _response_created(self, event: Any) -> None:
        response = _get(event, "response")
        response_id = str(_get(response, "id") or "")
        if not response_id:
            return
        metadata = _get(response, "metadata") or {}
        client_turn_id = str(_get(metadata, "native_client_turn_id") or "")
        phase = str(_get(metadata, "native_phase") or "authorize")
        turn = next(
            (t for t in reversed(self._turns.values()) if t.client_turn_id == client_turn_id),
            None,
        ) if client_turn_id else next(
            (
                t for t in self._turns.values()
                if not t.finished and "authorize" not in t.response_phases.values()
            ),
            None,
        )
        if turn is None:
            logger.warning("native_response_orphan session_id=%s response_id=%s", self.session_id, response_id)
            return
        turn.response_phases[response_id] = phase
        self._response_turns[response_id] = turn
        if phase == "speak":
            self._session.arm_native_response(response_id)
        logger.info(
            "native_response_created session_id=%s client_turn_id=%s response_id=%s phase=%s",
            self.session_id, turn.client_turn_id, response_id, phase,
        )

    async def _handle_tool_call(self, event: Any) -> None:
        call_id = str(_get(event, "call_id") or "")
        if not call_id or call_id in self._handled_calls:
            return
        self._handled_calls.add(call_id)
        response_id = str(_get(event, "response_id") or self._call_responses.get(call_id) or "")
        name = str(_get(event, "name") or self._call_names.get(call_id) or "")
        turn = self._response_turns.get(response_id)
        if turn is None:
            await self._tool_error(call_id, response_id, "tool call is not correlated to this interview turn")
            return
        try:
            await asyncio.wait_for(turn.transcript_ready.wait(), timeout=_TOOL_TIMEOUT_SECONDS)
            args = json.loads(str(_get(event, "arguments") or "{}"))
            if not isinstance(args, dict):
                raise native_agent.NativeAgentAuthorizationError("tool arguments must be an object")
            logger.info(
                "native_tool_requested session_id=%s client_turn_id=%s response_id=%s tool=%s",
                self.session_id, turn.client_turn_id, response_id, name,
            )
            if name == NATIVE_ALLOWED_FACTS_TOOL:
                authorization = await asyncio.get_running_loop().run_in_executor(
                    None, self._authorize_sync, turn,
                )
                turn.authorization = authorization
                output = authorization.tool_payload()
                followup = self._stage_response_event(turn)
            elif name == NATIVE_STAGE_RESPONSE_TOOL:
                if turn.authorization is None:
                    raise native_agent.NativeAgentAuthorizationError("facts were not authorized")
                staged = native_agent.stage_patient_response(
                    turn.authorization,
                    authorization_id=str(args.get("authorization_id") or ""),
                    patient_text=str(args.get("patient_text") or ""),
                    used_fact_ids=args.get("used_fact_ids"),
                )
                turn.staged = staged
                output = {"approved": True, "patient_text": staged.patient_text}
                followup = self._speak_response_event(turn, staged.patient_text)
            else:
                raise native_agent.NativeAgentAuthorizationError("unknown patient tool")
            await self._session.send_tool_output(
                call_id=call_id,
                output=json.dumps(output, separators=(",", ":")),
                after_response_id=response_id,
                followup=followup,
            )
            logger.info(
                "native_tool_result_returned session_id=%s client_turn_id=%s response_id=%s tool=%s allowed=true",
                self.session_id, turn.client_turn_id, response_id, name,
            )
        except Exception as exc:
            logger.warning(
                "native_tool_rejected session_id=%s client_turn_id=%s response_id=%s tool=%s error=%s",
                self.session_id, turn.client_turn_id, response_id, name or "unknown", type(exc).__name__,
            )
            await self._tool_error(call_id, response_id, "patient information is unavailable")
            turn.finished = True
            self._on_status(turn.client_turn_id, "failed")

    def _authorize_sync(self, turn: _Turn) -> native_agent.FactAuthorization:
        db = self._db_factory()
        try:
            return native_agent.authorize_patient_facts(
                db,
                session_id=self.session_id,
                case_id=self.case_id,
                client_turn_id=turn.client_turn_id,
                question=turn.text,
            )
        finally:
            db.close()

    async def _tool_error(self, call_id: str, response_id: str, message: str) -> None:
        await self._session.send_tool_output(
            call_id=call_id,
            output=native_agent.safe_tool_error(message),
            after_response_id=response_id,
            followup=None,
        )

    async def _handle_audio(self, event: Any) -> None:
        response_id = str(_get(event, "response_id") or "")
        if self._session.is_native_response_cancelled(response_id):
            return
        turn = self._response_turns.get(response_id)
        if turn is None or turn.finished or turn.staged is None or self._phase(turn, event) != "speak":
            return
        if turn.epoch is None or not self._generation_is_current(turn.epoch):
            await self._session.cancel_native_response(response_id)
            return
        import base64
        try:
            pcm = base64.b64decode(str(_get(event, "delta") or ""))
        except Exception:
            logger.warning("native_audio_decode_failed session_id=%s response_id=%s", self.session_id, response_id)
            return
        if not pcm:
            return
        self._session.note_native_audio(
            response_id, str(_get(event, "item_id") or ""), len(pcm),
        )
        if turn.first_audio_at is None:
            turn.first_audio_at = time.monotonic()
            self._on_speaking_started(turn.client_turn_id, turn.staged.patient_text)
            logger.info(
                "native_first_audio_delta session_id=%s client_turn_id=%s response_id=%s",
                self.session_id, turn.client_turn_id, response_id,
            )
        turn.audio_bytes += len(pcm)
        await self._on_audio(pcm)
        if turn.audio_bytes == len(pcm):
            logger.info(
                "native_first_livekit_audio_frame session_id=%s client_turn_id=%s response_id=%s",
                self.session_id, turn.client_turn_id, response_id,
            )

    async def _response_done(self, event: Any) -> None:
        response = _get(event, "response")
        response_id = str(_get(response, "id") or _get(event, "response_id") or "")
        turn = self._response_turns.get(response_id)
        if turn is None or self._phase(turn, event) != "speak" or turn.finished:
            return
        turn.finished = True
        self._session.disarm_native_response(response_id)
        status = str(_get(response, "status") or "completed")
        provider_completed = status == "completed" and not self._session.is_native_response_cancelled(response_id)
        delivered = (turn.final_transcript or "".join(turn.transcript_parts)).strip()
        if not delivered or not turn.audio_bytes or turn.staged is None or turn.epoch is None:
            self._on_status(turn.client_turn_id, "interrupted" if status == "cancelled" else "failed")
            return
        try:
            fidelity_ok = _normalize_spoken(delivered) == _normalize_spoken(turn.staged.patient_text)
            completed = provider_completed and fidelity_ok
            reason = (
                "complete" if completed
                else "delivery_mismatch" if provider_completed
                else "interrupted"
            )
            persisted = await asyncio.get_running_loop().run_in_executor(
                None, self._persist_patient_sync, turn, delivered, completed, reason,
            )
        except native_agent.NativeAgentStaleError:
            logger.info(
                "native_stale_response_discarded session_id=%s client_turn_id=%s response_id=%s",
                self.session_id, turn.client_turn_id, response_id,
            )
            return
        except Exception:
            logger.exception(
                "native_patient_persist_failed session_id=%s client_turn_id=%s response_id=%s",
                self.session_id, turn.client_turn_id, response_id,
            )
            self._on_status(turn.client_turn_id, "failed")
            return
        turn.persisted_patient_id = persisted.patient_turn_id
        self._on_patient_final(turn.client_turn_id, turn.epoch, persisted, reason)
        self._on_status(
            turn.client_turn_id,
            "speaking_ended" if completed else "interrupted" if not provider_completed else "failed",
        )
        logger.info(
            "native_response_completed session_id=%s client_turn_id=%s response_id=%s status=%s audio_bytes=%d",
            self.session_id, turn.client_turn_id, response_id, status, turn.audio_bytes,
        )

    def _persist_patient_sync(
        self, turn: _Turn, delivered: str, completed: bool, reason: str,
    ) -> native_agent.PersistedNativePatientTurn:
        db = self._db_factory()
        try:
            assert turn.staged is not None and turn.epoch is not None
            return native_agent.persist_delivered_patient_turn(
                db,
                staged=turn.staged,
                delivered_text=delivered,
                completed=completed,
                delivery_reason=reason,
                model_name=self.model_name,
                is_generation_valid=lambda: self._generation_is_current(turn.epoch),
                generation_authority=self._generation_authority,
            )
        finally:
            db.close()

    def _response_turn(self, event: Any) -> _Turn | None:
        response_id = str(_get(event, "response_id") or "")
        if not response_id:
            response = _get(event, "response")
            response_id = str(_get(response, "id") or "")
        return self._response_turns.get(response_id)

    def _phase(self, turn: _Turn, event: Any) -> str:
        response_id = str(_get(event, "response_id") or "")
        if not response_id:
            response_id = str(_get(_get(event, "response"), "id") or "")
        return turn.response_phases.get(response_id, "")

    def _authorize_response_event(self, turn: _Turn) -> dict:
        return {
            "type": "response.create",
            "response": {
                "conversation": "auto",
                "output_modalities": ["audio"],
                "metadata": {
                    "native_client_turn_id": turn.client_turn_id,
                    "native_phase": "authorize",
                },
                "tool_choice": {"type": "function", "name": NATIVE_ALLOWED_FACTS_TOOL},
            },
        }

    def _stage_response_event(self, turn: _Turn) -> dict:
        return {
            "type": "response.create",
            "response": {
                "conversation": "auto",
                "output_modalities": ["audio"],
                "metadata": {
                    "native_client_turn_id": turn.client_turn_id,
                    "native_phase": "stage",
                },
                "tool_choice": {"type": "function", "name": NATIVE_STAGE_RESPONSE_TOOL},
                "instructions": (
                    "Using only the authorized tool result, compose the concise natural patient "
                    "answer. Do not speak yet. Call stage_patient_response with the wording and "
                    "the exact authorized fact IDs actually used."
                ),
            },
        }

    def _speak_response_event(self, turn: _Turn, text: str) -> dict:
        return {
            "type": "response.create",
            "response": {
                "conversation": "auto",
                "output_modalities": ["audio"],
                "metadata": {
                    "native_client_turn_id": turn.client_turn_id,
                    "native_phase": "speak",
                },
                "tool_choice": "none",
                "instructions": (
                    "Speak exactly this approved patient wording, without adding facts or commentary: "
                    + json.dumps(text)
                ),
            },
        }

    def _trim(self) -> None:
        while len(self._turns) > _MAX_FINISHED:
            key, turn = next(iter(self._turns.items()))
            if not turn.finished:
                break
            self._turns.pop(key, None)


def _normalize_spoken(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())
