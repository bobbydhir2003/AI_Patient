"""Persistent LiveKit Agents worker - Phase 2.

Replaces Phase 1's single-room, manually-invoked CLI script
(`python -m app.livekit_agent.worker --room ... --session-id ... --case-id
...`) with a long-running process built on the real LiveKit Agents
job-dispatch framework (`livekit-agents` - see backend/requirements.txt for
why it is PINNED to 1.3.5, not "latest"). Started ONCE (by systemd - see
docs/DEPLOYMENT.md), it registers with LiveKit and receives one JOB per
student interview automatically:

    Start Interview (browser)
        -> POST /api/livekit/token mints a token with an EXPLICIT agent
           dispatch entry embedded (see livekit_token_service.py:
           RoomConfiguration/RoomAgentDispatch, agent_name=
           settings.livekit_agent_name - a fixed, server-controlled value)
        -> the student's browser creates the room
        -> LiveKit automatically sends THIS worker a job request carrying
           {"session_id": ..., "case_id": ...} as JSON job metadata
        -> entrypoint() below receives a JobContext already carrying a REAL
           connected livekit.rtc.Room (ctx.room) - no --room/--session-id/
           --case-id args, nothing copied by hand.

No SSH command, no copying a room name into a terminal. Everything below
entrypoint()'s metadata parsing is UNCHANGED from Phase 1's turn-handling
logic (PocAgentSession): same interview_slot()/tts_slot() semaphore reuse via
patient_adapter.py, same "patient_turn_status" data-channel signaling, same
one-persistent-audio-track-per-interview design.

Job isolation: the Agents framework runs each accepted job in its OWN
process by default (JobExecutorType.PROCESS - kept as the default; see
WorkerOptions below), so one interview crashing can never affect another, and
one PocAgentSession instance is constructed FRESH per job - there is no
module-level/global mutable state shared across interviews (session_id,
case_id, DB session factory, turn lock, and audio source are all
instance-scoped).

Room/job cleanup: verified against the ACTUAL installed API (not docs) that
JobContext does NOT automatically end a job when a participant leaves -
that behavior lives in the higher-level AgentSession/RoomIO voice pipeline,
which this POC does not use (it keeps its own turn logic). This module
therefore explicitly listens for "participant_disconnected" and calls
ctx.shutdown() itself - idempotent (a second disconnect event, or the
framework's own shutdown callback firing, is a no-op) - and deliberately
ignores a disconnect that reports the AGENT's own identity (defensive; our
own local participant is never delivered through this event, but the check
costs nothing and matches the explicit requirement).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Callable

from livekit.agents import AutoSubscribe, JobContext, JobRequest, WorkerOptions, cli

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.livekit_agent import patient_adapter

if TYPE_CHECKING:
    import livekit.rtc as rtc

logger = get_logger("app.livekit_agent.worker")

STUDENT_TEXT_TOPIC = "student_text"
# Fixed identity our worker always joins under - matches the constant the
# frontend (livekitPocEngine.ts's AGENT_IDENTITY) already checks for, so the
# "Agent connected" diagnostic keeps working with ZERO frontend changes. Set
# explicitly in _handle_job_request below (the framework's default identity
# is "agent-<job_id>", which would silently break that check).
AGENT_PARTICIPANT_IDENTITY = "patient-agent"

# 20ms frames at 16kHz mono 16-bit PCM = 640 bytes/frame - a conventional
# WebRTC frame duration. Unchanged from Phase 1.
_FRAME_SECONDS = 0.02
_FRAME_BYTES = int(patient_adapter.LIVEKIT_PCM_SAMPLE_RATE * _FRAME_SECONDS) * 2


def parse_job_metadata(raw: str) -> tuple[str, str] | None:
    """Extract (session_id, case_id) from a job's JSON metadata string (see
    livekit_token_service.py's RoomAgentDispatch(metadata=...)). Returns None
    for anything malformed/incomplete - the caller must fail closed, never
    guess a session or case id."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    session_id = str(data.get("session_id") or "").strip()
    case_id = str(data.get("case_id") or "").strip()
    if not session_id or not case_id:
        return None
    return session_id, case_id


class PocAgentSession:
    """Owns exactly ONE job's room interaction, ONE persistent outbound audio
    track, and ONE turn lock, so two overlapping student messages can never
    trigger two simultaneous/overlapping patient responses (mirrors the
    frontend's own single-active-generation guard, patientVoiceService.ts's
    guard). A fresh instance is constructed per job (see entrypoint()) - no
    state here is ever shared across interviews.

    Barge-in/interruption is explicitly OUT of scope for this POC (see the
    Phase 1 final report) - a student message that arrives while a patient
    turn is already in flight is dropped with a log line, not queued or used
    to interrupt playback.
    """

    def __init__(
        self,
        *,
        room: "rtc.Room",
        session_id: str,
        case_id: str,
        on_shutdown: Callable[[str], None],
    ) -> None:
        self._room = room
        self.session_id = session_id
        self.case_id = case_id
        self._on_shutdown = on_shutdown
        self._shutdown_called = False  # idempotency guard - see _trigger_shutdown
        self._turn_lock = asyncio.Lock()
        self._session_factory = get_db_factory()
        self._audio_source: "rtc.AudioSource | None" = None

    async def start(self) -> None:
        """Wire room event handlers and publish the ONE persistent audio
        track for this job's entire lifetime. The room is already connected
        (JobContext.connect() was awaited by the caller) - this method never
        connects/disconnects the room itself; that is the framework's job."""
        import livekit.rtc as rtc

        room = self._room

        @room.on("data_received")
        def _on_data(packet: "rtc.DataPacket") -> None:
            if packet.topic != STUDENT_TEXT_TOPIC:
                return
            try:
                payload = json.loads(packet.data.decode("utf-8"))
            except Exception:
                logger.warning("livekit_agent_bad_payload session_id=%s", self.session_id)
                return
            text = str(payload.get("text") or "").strip()
            client_turn_id = str(payload.get("clientTurnId") or "")
            if not text or not client_turn_id:
                return
            asyncio.ensure_future(self._handle_student_turn(text, client_turn_id))

        @room.on("participant_disconnected")
        def _on_participant_left(participant: object) -> None:
            identity = getattr(participant, "identity", "?")
            logger.info("livekit_agent_participant_left session_id=%s identity=%s", self.session_id, identity)
            # Only the STUDENT leaving ends the job - never our own agent
            # identity (see the module docstring: defensive, not load-bearing
            # today, since a local participant never fires this event for
            # itself, but explicit per the isolation requirement).
            if identity == AGENT_PARTICIPANT_IDENTITY:
                return
            self._trigger_shutdown("student_left")

        self._audio_source = rtc.AudioSource(
            sample_rate=patient_adapter.LIVEKIT_PCM_SAMPLE_RATE, num_channels=1,
        )
        track = rtc.LocalAudioTrack.create_audio_track("patient-voice", self._audio_source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions())
        logger.info("livekit_agent_track_published session_id=%s", self.session_id)

    def _trigger_shutdown(self, reason: str) -> None:
        """Idempotent: a second disconnect event (or the framework's own
        shutdown callback firing for an unrelated reason) must never raise or
        double-signal - the job must never be left orphaned OR crash on a
        redundant cleanup attempt."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("livekit_agent_job_shutdown session_id=%s reason=%s", self.session_id, reason)
        self._on_shutdown(reason)

    async def _handle_student_turn(self, text: str, client_turn_id: str) -> None:
        if self._turn_lock.locked():
            logger.info("livekit_agent_turn_dropped_busy session_id=%s client_turn_id=%s", self.session_id, client_turn_id)
            return
        async with self._turn_lock:
            await self._run_turn(text, client_turn_id)

    async def _run_turn(self, text: str, client_turn_id: str) -> None:
        loop = asyncio.get_running_loop()
        # Best-effort per-turn latency breakdown for real-device validation -
        # stage name -> monotonic timestamp, logged as ONE line at the end.
        # Never includes patient text, audio bytes, or any secret.
        stages: list[tuple[str, float]] = [("turn_received", time.monotonic())]

        def on_stage(name: str) -> None:
            stages.append((name, time.monotonic()))

        try:
            # generate_and_persist_turn is synchronous (sync SQLAlchemy
            # session, sync interview_slot()/OpenAI call) - run it off the
            # event loop exactly like FastAPI's own sync route handlers run
            # in a threadpool, so it never blocks room/data-channel processing.
            result = await loop.run_in_executor(
                None, self._generate_turn_sync, text, client_turn_id, on_stage
            )
        except patient_adapter.LiveKitPocSessionNotFoundError:
            logger.error("livekit_agent_session_not_found session_id=%s", self.session_id)
            self._send_turn_status(client_turn_id, "failed")
            return
        except Exception:
            logger.exception("livekit_agent_generation_failed session_id=%s client_turn_id=%s", self.session_id, client_turn_id)
            self._send_turn_status(client_turn_id, "failed")
            return
        on_stage("persisted")

        pcm = await loop.run_in_executor(
            None,
            lambda: patient_adapter.synthesize_patient_audio_pcm(
                case_id=self.case_id, text=result.patient_text, on_stage=on_stage
            ),
        )
        if pcm is None:
            # Deliberately NOT falling back to any other TTS here - the POC
            # must surface a real failure, not silently degrade to legacy
            # browser TTS. The frontend surfaces its own diagnostic error
            # state on receiving this "failed" status (LiveKitTestPage).
            logger.error("livekit_agent_tts_failed session_id=%s client_turn_id=%s", self.session_id, client_turn_id)
            self._send_turn_status(client_turn_id, "failed")
            self._log_turn_timing(client_turn_id, stages)
            return

        # A continuously-open WebRTC track has no natural "clip ended" event
        # the way a file-backed <audio> element does - the frontend cannot
        # reliably infer turn boundaries from element events alone. Signal
        # them explicitly via the data channel so LiveKitTestPage's state
        # machine (THINKING -> SPEAKING -> LISTENING) has an unambiguous
        # source of truth.
        on_stage("first_audio_publish_start")
        self._send_turn_status(client_turn_id, "speaking_started")
        await self._publish_pcm(pcm)
        on_stage("speech_complete")
        self._send_turn_status(client_turn_id, "speaking_ended")
        self._log_turn_timing(client_turn_id, stages)
        logger.info("livekit_agent_turn_audio_published session_id=%s client_turn_id=%s bytes=%d", self.session_id, client_turn_id, len(pcm))

    @staticmethod
    def _log_turn_timing(client_turn_id: str, stages: list[tuple[str, float]]) -> None:
        """ONE structured log line per turn: stage=+NNNms relative to
        turn_received, for real-device latency validation. Never logs patient
        text, audio bytes, or secrets - stage names and elapsed ms only."""
        if len(stages) < 2:
            return
        t0 = stages[0][1]
        breakdown = " ".join(f"{name}=+{round((t - t0) * 1000)}ms" for name, t in stages[1:])
        logger.info("livekit_agent_turn_timing client_turn_id=%s %s", client_turn_id, breakdown)

    def _send_turn_status(self, client_turn_id: str, status: str) -> None:
        payload = json.dumps({"clientTurnId": client_turn_id, "status": status}).encode("utf-8")
        try:
            asyncio.ensure_future(
                self._room.local_participant.publish_data(payload, reliable=True, topic="patient_turn_status")
            )
        except Exception:
            logger.exception("livekit_agent_status_publish_failed session_id=%s client_turn_id=%s status=%s", self.session_id, client_turn_id, status)

    def _generate_turn_sync(
        self, text: str, client_turn_id: str, on_stage
    ) -> patient_adapter.PocTurnResult:
        db = self._session_factory()
        try:
            return patient_adapter.generate_and_persist_turn(
                db, session_id=self.session_id, case_id=self.case_id,
                question=text, client_turn_id=client_turn_id, on_stage=on_stage,
            )
        finally:
            db.close()

    async def _publish_pcm(self, pcm: bytes) -> None:
        import livekit.rtc as rtc

        assert self._audio_source is not None
        for i in range(0, len(pcm), _FRAME_BYTES):
            chunk = pcm[i : i + _FRAME_BYTES]
            if len(chunk) < 2:
                break
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=patient_adapter.LIVEKIT_PCM_SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await self._audio_source.capture_frame(frame)


async def _handle_job_request(request: JobRequest) -> None:
    """Accept every job dispatched to us under our fixed agent_name, joining
    with a FIXED, predictable identity (AGENT_PARTICIPANT_IDENTITY) rather
    than the framework's default "agent-<job_id>" - see that constant's
    docstring for why."""
    await request.accept(identity=AGENT_PARTICIPANT_IDENTITY, name="PT AI Patient")


async def entrypoint(ctx: JobContext) -> None:
    """One call per accepted job = one student interview. Everything here is
    scoped to THIS job - no module-level dict/cache keyed by session_id, no
    state that could leak between two concurrent interviews (see the module
    docstring's isolation notes)."""
    parsed = parse_job_metadata(ctx.job.metadata)
    if parsed is None:
        logger.error("livekit_agent_job_missing_metadata job_id=%s", ctx.job.id)
        ctx.shutdown(reason="missing_or_invalid_metadata")
        return
    session_id, case_id = parsed

    # SUBSCRIBE_NONE: this worker never processes the student's raw mic audio
    # (transcription happens client-side via the browser's Web Speech API and
    # arrives as a "student_text" data message) - subscribing to audio tracks
    # here would be pure waste.
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)
    logger.info(
        "livekit_agent_job_connected job_id=%s session_id=%s case_id=%s room=%s",
        ctx.job.id, session_id, case_id, ctx.room.name,
    )

    done = asyncio.Event()

    def _on_session_shutdown(reason: str) -> None:
        ctx.shutdown(reason=reason)
        done.set()

    async def _on_ctx_shutdown(reason: str) -> None:
        # Safety net: if the framework itself ends the job for a reason our
        # own participant_disconnected handler never saw (e.g. a drain/
        # timeout), make sure entrypoint() still returns instead of hanging
        # forever on done.wait().
        done.set()

    ctx.add_shutdown_callback(_on_ctx_shutdown)

    poc_session = PocAgentSession(
        room=ctx.room, session_id=session_id, case_id=case_id, on_shutdown=_on_session_shutdown,
    )
    await poc_session.start()

    # Block here for the job's entire lifetime - returning from entrypoint()
    # ends the job, so this await is what keeps the interview's room/track
    # alive until the student leaves (or the framework shuts us down).
    await done.wait()


def _build_worker_options() -> WorkerOptions:
    settings = get_settings()
    if not (
        settings.livekit_poc_enabled
        and settings.livekit_url
        and settings.livekit_api_key
        and settings.livekit_api_secret
    ):
        raise SystemExit(
            "LIVEKIT_POC_ENABLED / LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are "
            "not fully set. This worker will not start without real LiveKit Cloud "
            "credentials AND LIVEKIT_POC_ENABLED=true - see backend/.env.example."
        )
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=_handle_job_request,
        # Explicit dispatch ONLY - setting agent_name means we are NEVER
        # auto-dispatched to a room we weren't explicitly invited to (see
        # livekit_token_service.py's RoomAgentDispatch, the only caller that
        # can invite this agent, gated by the SAME require_admin +
        # user_can_access_session ownership check every other session-scoped
        # endpoint uses).
        agent_name=settings.livekit_agent_name,
        # Explicit, not the framework's own os.environ fallback - single
        # source of truth stays app.core.config.get_settings(), exactly like
        # every other provider credential in this codebase.
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


if __name__ == "__main__":
    # Persistent process - started ONCE by systemd (ptai-livekit-agent.service,
    # see docs/DEPLOYMENT.md), never per-interview, never with --room/
    # --session-id/--case-id. Run via:
    #   python -m app.livekit_agent.worker start
    cli.run_app(_build_worker_options())
