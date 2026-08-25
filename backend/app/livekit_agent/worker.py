"""Standalone LiveKit POC agent worker - Phase 1 ONLY.

Runs as its OWN process, never inside Uvicorn/FastAPI request workers (see
the LiveKit feasibility audit: agent work must not compete with or destabilize
the request-serving process). Independently startable for local testing:

    cd backend
    source .venv/bin/activate
    export LIVEKIT_URL=wss://your-project.livekit.cloud
    export LIVEKIT_API_KEY=...
    export LIVEKIT_API_SECRET=...
    python -m app.livekit_agent.worker --room ptai-poc-<session_id> \
        --session-id <session_id> --case-id carly

The room name and session id come from POST /api/livekit/token's response
(see app/api/livekit.py) - a developer starts the frontend POC page first,
copies the room name it was issued, then starts this worker pointed at that
same room.

Joins the room as participant identity "patient-agent", listens for a
student's recognized speech as a data message (topic="student_text",
JSON: {"text": ..., "clientTurnId": ...}), and for each one:

    interview_slot()  -> generate_patient_response()  -> persist transcript
    tts_slot()         -> ElevenLabsClient (PCM output) -> publish audio frames
                                                            on ONE persistent
                                                            LiveKit audio track

All patient-generation and TTS/voice logic is reused via patient_adapter.py -
see that module's docstring for the exact list of production components
reused and the two Redis semaphores this process participates in exactly like
every FastAPI worker does. Nothing here re-implements prompt logic or talks
to ElevenLabs/OpenAI directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.livekit_agent import patient_adapter

if TYPE_CHECKING:
    import livekit.rtc as rtc

logger = get_logger("app.livekit_agent.worker")

STUDENT_TEXT_TOPIC = "student_text"
AGENT_IDENTITY = "patient-agent"

# 20ms frames at 16kHz mono 16-bit PCM = 640 bytes/frame - a conventional
# WebRTC frame duration.
_FRAME_SECONDS = 0.02
_FRAME_BYTES = int(patient_adapter.LIVEKIT_PCM_SAMPLE_RATE * _FRAME_SECONDS) * 2


def _build_agent_token(room_name: str) -> str:
    """The agent mints its OWN room-join token using the SAME server-side
    credentials livekit_token_service.py uses for the student's token - never
    the frontend's token, never a client-supplied value."""
    from livekit.api import AccessToken, VideoGrants

    settings = get_settings()
    grants = VideoGrants(
        room_join=True, room=room_name, can_publish=True, can_subscribe=True, can_publish_data=True,
    )
    return (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(AGENT_IDENTITY)
        .with_name("PT AI Patient (POC)")
        .with_grants(grants)
        .with_ttl(timedelta(hours=6))  # long-lived: the agent stays connected for the whole POC session
        .to_jwt()
    )


class PocAgentSession:
    """Owns exactly one room connection, one persistent outbound audio track,
    and one turn lock, so two overlapping student messages can never trigger
    two simultaneous/overlapping patient responses (mirrors the frontend's
    own single-active-generation guard, patientVoiceService.ts's guard).

    Barge-in/interruption is explicitly OUT of scope for this Phase 1 POC
    (see the final report) - a student message that arrives while a patient
    turn is already in flight is dropped with a log line, not queued or
    used to interrupt playback.
    """

    def __init__(self, *, room_name: str, session_id: str, case_id: str) -> None:
        self.room_name = room_name
        self.session_id = session_id
        self.case_id = case_id
        self._turn_lock = asyncio.Lock()
        self._session_factory = get_db_factory()
        self._audio_source: "rtc.AudioSource | None" = None
        self._room: "rtc.Room | None" = None

    async def run(self) -> None:
        import livekit.rtc as rtc

        settings = get_settings()
        room = rtc.Room()
        self._room = room

        @room.on("data_received")
        def _on_data(packet: "rtc.DataPacket") -> None:
            if packet.topic != STUDENT_TEXT_TOPIC:
                return
            try:
                payload = json.loads(packet.data.decode("utf-8"))
            except Exception:
                logger.warning("livekit_poc_agent_bad_payload room=%s", self.room_name)
                return
            text = str(payload.get("text") or "").strip()
            client_turn_id = str(payload.get("clientTurnId") or "")
            if not text or not client_turn_id:
                return
            asyncio.ensure_future(self._handle_student_turn(text, client_turn_id))

        @room.on("participant_disconnected")
        def _on_participant_left(participant: object) -> None:
            identity = getattr(participant, "identity", "?")
            logger.info("livekit_poc_agent_participant_left room=%s identity=%s", self.room_name, identity)

        token = _build_agent_token(self.room_name)
        await room.connect(settings.livekit_url, token)
        logger.info("livekit_poc_agent_connected room=%s identity=%s", self.room_name, AGENT_IDENTITY)

        # ONE persistent audio source/track for the WHOLE session, published
        # ONCE - subsequent turns push new frames into the SAME source, never
        # publish a new track. This is the exact structural property (one
        # long-lived track vs. a fresh player per turn) the mobile/LiveKit
        # feasibility audits identified as the point of this experiment.
        self._audio_source = rtc.AudioSource(
            sample_rate=patient_adapter.LIVEKIT_PCM_SAMPLE_RATE, num_channels=1,
        )
        track = rtc.LocalAudioTrack.create_audio_track("patient-voice", self._audio_source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions())
        logger.info("livekit_poc_agent_track_published room=%s", self.room_name)

        await self._wait_for_stop_signal()
        await room.disconnect()
        logger.info("livekit_poc_agent_disconnected room=%s", self.room_name)

    @staticmethod
    async def _wait_for_stop_signal() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows: no add_signal_handler; Ctrl+C still raises KeyboardInterrupt
        await stop_event.wait()

    async def _handle_student_turn(self, text: str, client_turn_id: str) -> None:
        if self._turn_lock.locked():
            logger.info("livekit_poc_agent_turn_dropped_busy client_turn_id=%s", client_turn_id)
            return
        async with self._turn_lock:
            await self._run_turn(text, client_turn_id)

    async def _run_turn(self, text: str, client_turn_id: str) -> None:
        loop = asyncio.get_running_loop()
        # Best-effort per-turn latency breakdown for real-device validation
        # (see the Phase 1 validation plan) - stage name -> monotonic
        # timestamp, logged as ONE line at the end. Never includes patient
        # text, audio bytes, or any secret - just stage names and elapsed ms.
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
            logger.error("livekit_poc_agent_session_not_found session_id=%s", self.session_id)
            self._send_turn_status(client_turn_id, "failed")
            return
        except Exception:
            logger.exception("livekit_poc_agent_generation_failed client_turn_id=%s", client_turn_id)
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
            # browser TTS (that would hide exactly what this experiment is
            # trying to measure). The frontend surfaces its own diagnostic
            # error state on receiving this "failed" status (LiveKitTestPage).
            logger.error("livekit_poc_agent_tts_failed client_turn_id=%s", client_turn_id)
            self._send_turn_status(client_turn_id, "failed")
            self._log_turn_timing(client_turn_id, stages)
            return

        # A continuously-open WebRTC track has no natural "clip ended" event
        # the way a file-backed <audio> element does - the frontend cannot
        # reliably infer turn boundaries from element events alone. Signal
        # them explicitly via the data channel so LiveKitTestPage's state
        # machine (THINKING -> SPEAKING -> LISTENING) has an unambiguous
        # source of truth, matching how the legacy engine's onplay/onended
        # already drive its own state machine.
        on_stage("first_audio_publish_start")
        self._send_turn_status(client_turn_id, "speaking_started")
        await self._publish_pcm(pcm)
        on_stage("speech_complete")
        self._send_turn_status(client_turn_id, "speaking_ended")
        self._log_turn_timing(client_turn_id, stages)
        logger.info("livekit_poc_agent_turn_audio_published client_turn_id=%s bytes=%d", client_turn_id, len(pcm))

    @staticmethod
    def _log_turn_timing(client_turn_id: str, stages: list[tuple[str, float]]) -> None:
        """ONE structured log line per turn: stage=+NNNms relative to
        turn_received, for real-device latency validation. Never logs patient
        text, audio bytes, or secrets - stage names and elapsed ms only."""
        if len(stages) < 2:
            return
        t0 = stages[0][1]
        breakdown = " ".join(f"{name}=+{round((t - t0) * 1000)}ms" for name, t in stages[1:])
        logger.info("livekit_poc_turn_timing client_turn_id=%s %s", client_turn_id, breakdown)

    def _send_turn_status(self, client_turn_id: str, status: str) -> None:
        if self._room is None:
            return
        payload = json.dumps({"clientTurnId": client_turn_id, "status": status}).encode("utf-8")
        try:
            asyncio.ensure_future(
                self._room.local_participant.publish_data(payload, reliable=True, topic="patient_turn_status")
            )
        except Exception:
            logger.exception("livekit_poc_agent_status_publish_failed client_turn_id=%s status=%s", client_turn_id, status)

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


async def _amain() -> None:
    parser = argparse.ArgumentParser(description="PT AI Patient - LiveKit POC agent worker (Phase 1 only)")
    parser.add_argument("--room", required=True, help="Room name, e.g. ptai-poc-<session_id>")
    parser.add_argument("--session-id", required=True, help="Existing InterviewSession id (POC/admin session)")
    parser.add_argument("--case-id", default="carly", help="Existing case id (default: carly)")
    args = parser.parse_args()

    settings = get_settings()
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise SystemExit(
            "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not set. "
            "This worker will not start without real LiveKit Cloud credentials - "
            "see backend/.env.example and the Phase 1 POC report."
        )

    # Timestamped to match app/core/logging.py's format - lets real-device
    # testing correlate agent log lines with backend/frontend timestamps.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    session = PocAgentSession(room_name=args.room, session_id=args.session_id, case_id=args.case_id)
    await session.run()


if __name__ == "__main__":
    asyncio.run(_amain())
