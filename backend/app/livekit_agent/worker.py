"""Persistent LiveKit Agents worker.

Built on the real LiveKit Agents job-dispatch framework (`livekit-agents` -
see backend/requirements.txt for why it is PINNED to 1.3.5, not "latest").
Started ONCE (by systemd - see docs/DEPLOYMENT.md), it registers with
LiveKit and receives one JOB per student interview automatically:

    Start Interview (browser)
        -> POST /api/livekit/token (admin) or
           POST /api/interviews/{id}/livekit-token (student) mints a token
           with an EXPLICIT agent dispatch entry embedded (see
           livekit_token_service.py: RoomConfiguration/RoomAgentDispatch,
           agent_name=settings.livekit_agent_name - a fixed, server-controlled
           value)
        -> the student's browser creates the room
        -> LiveKit automatically sends a job request, to ANY currently
           registered worker process sharing this agent_name, carrying
           {"session_id": ..., "case_id": ...} as JSON job metadata (this is
           the ONLY thing that needs to be true for horizontal scaling - see
           the module docstring's "Horizontal scaling" section below)
        -> entrypoint() below receives a JobContext already carrying a REAL
           connected livekit.rtc.Room (ctx.room) - no --room/--session-id/
           --case-id args, nothing copied by hand.

Job isolation: the Agents framework runs each accepted job in its OWN
process by default (JobExecutorType.PROCESS - kept as the default; see
WorkerOptions below), so one interview crashing can never affect another, and
one PocAgentSession instance is constructed FRESH per job - there is no
module-level/global mutable state shared across interviews (session_id,
case_id, job_id, room_id, DB session factory, turn lock, dedup tracking, and
audio source are all instance-scoped).

Production reliability protocol (see also src/services/livekit/
livekitPocEngine.ts's matching docstring): a confirmed production incident
showed a student's browser could enter LISTENING and publish a student_text
packet BEFORE this worker's job process had even joined the room - LiveKit's
"reliable" data delivery only guarantees delivery to participants already
present, so the publish resolved successfully while reaching zero
recipients, and the turn silently vanished (no error, no retry, nothing).
This module now implements the agent side of a full recovery protocol:
  1. An explicit "agent_ready" control message (topic "agent_control"),
     sent only once the room is joined, the student_text data handler is
     installed, and the session has been verified to exist in the DB - the
     browser will not leave WAITING_FOR_AGENT until it sees this.
  2. An immediate "turn_ack" (topic "agent_control") for every valid
     student_text packet, sent BEFORE any OpenAI/TTS work starts, so the
     browser can distinguish "the SDK accepted my publish" from "the agent
     actually received my turn" - the browser retries automatically (same
     clientTurnId, bounded) if no ack arrives in time.
  3. Idempotency: a clientTurnId already in flight or already completed is
     ack'd again but never reprocessed, so a browser-side automatic retry
     (or any other duplicate) can never generate two patient responses or
     bill OpenAI/ElevenLabs twice for the same turn.
  4. Every failure path (session-not-found, OpenAI exception, ElevenLabs/TTS
     exception, or a clean "no capacity" signal) is caught, logged with
     session_id/client_turn_id/job_id/room_id, and always results in an
     explicit patient_turn_status "failed" message - no exception is ever
     allowed to disappear as an untracked "Task exception was never
     retrieved" warning (the failure mode a prior forensic inspection
     identified as a real, silent-turn-loss risk).
There is deliberately NO student-facing retry affordance anywhere in this
protocol - all recovery is internal (browser-side automatic resend, or an
explicit "failed" status the student sees as a normal, if disappointing,
outcome - never a dead end requiring a manual retry click).

Horizontal scaling: this worker process is dispatched to purely by
agent_name (a fixed string, see WorkerOptions.agent_name below) - LiveKit
Cloud's own dispatcher balances jobs across EVERY currently-registered
worker process sharing that name, using each worker's self-reported load
(WorkerOptions.load_threshold). Running N identical copies of this process
(on one machine or spread across several) is therefore the intended,
zero-code-change scaling mechanism - see docs/DEPLOYMENT.md's "LiveKit
horizontal scaling" section for the concrete topology recommendation and the
remaining machine-local requirements (a SHARED Redis for
interview_slot()/tts_slot(), and a SHARED database - see
app/core/concurrency.py and app/core/config.py's database_url/redis_url).

Room/job cleanup: verified against the ACTUAL installed API (not docs) that
JobContext does NOT automatically end a job when a participant leaves -
that behavior lives in the higher-level AgentSession/RoomIO voice pipeline,
which this module does not use (it keeps its own turn logic). This module
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
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

from livekit.agents import AutoSubscribe, JobContext, JobRequest, WorkerOptions, cli

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.livekit_agent import patient_adapter
from app.repositories.session_repository import SessionRepository

if TYPE_CHECKING:
    import livekit.rtc as rtc

logger = get_logger("app.livekit_agent.worker")

STUDENT_TEXT_TOPIC = "student_text"
PATIENT_TURN_STATUS_TOPIC = "patient_turn_status"
# Control-plane messages (agent_ready, turn_ack) - distinct from
# patient_turn_status (turn/audio lifecycle) so the two concerns evolve
# independently. Both use topic + a `type`/`status` discriminator, matching
# the frontend's livekitPocEngine.ts AgentControlPayload/TurnStatusPayload.
AGENT_CONTROL_TOPIC = "agent_control"

# Phase D2: the browser sends this on AGENT_CONTROL_TOPIC (the SAME topic the
# agent already uses for agent_ready/turn_ack) to request true SPEAKING-only
# interruption ("barge-in"). See PocAgentSession._on_interrupt_patient for
# the correlation/validation rules and the module docstring's "Barge-in" note
# for why this is deliberately never honored before audio has started.
INTERRUPT_PATIENT_TYPE = "interrupt_patient"

# Fixed identity our worker always joins under - matches the constant the
# frontend (livekitPocEngine.ts's AGENT_IDENTITY) already checks for, so the
# "Agent connected" diagnostic keeps working with ZERO frontend changes. Set
# explicitly in _handle_job_request below (the framework's default identity
# is "agent-<job_id>", which would silently break that check). Safe to reuse
# across every concurrent job/room - identities only need to be unique
# WITHIN a room, and each interview has its own room.
AGENT_PARTICIPANT_IDENTITY = "patient-agent"

# 20ms frames at 16kHz mono 16-bit PCM = 640 bytes/frame - a conventional
# WebRTC frame duration. Unchanged from Phase 1.
_FRAME_SECONDS = 0.02
_FRAME_BYTES = int(patient_adapter.LIVEKIT_PCM_SAMPLE_RATE * _FRAME_SECONDS) * 2

# Bounds memory for the per-session completed-clientTurnId dedup set (see
# PocAgentSession._mark_turn_completed) - a typical interview has a few dozen
# turns at most, so this is a generous cap, not a tuned limit.
_MAX_COMPLETED_TURN_IDS = 200


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

    A student message arriving while a DIFFERENT patient turn is already in
    flight is dropped with a log line, not queued (still no message-queueing
    barge-in). A DUPLICATE of the SAME in-flight or already-completed
    clientTurnId is a different case, handled by the idempotency tracking
    below (_in_flight_turn_ids/_completed_turn_ids) - it is always ack'd, but
    never reprocessed.

    Phase D2: true SPEAKING-only interruption IS implemented (see
    _on_interrupt_patient) - the student can stop the CURRENT turn's audio
    mid-playback. Deliberately restricted to the audio-publish phase only:
    _active_turn_task tracks the in-flight turn's asyncio.Task so it can be
    cancelled, but the OpenAI/ElevenLabs calls inside it run via
    loop.run_in_executor (a real OS thread) - cancelling the asyncio Task
    while still awaiting the executor future stops the CALLER from waiting on
    it, but does NOT stop the underlying thread/HTTP call, which keeps
    running to completion with its result simply discarded. Interrupting
    during that phase would be a FAKE cancellation (provider cost still
    incurred, nothing actually stopped) - _speaking_client_turn_id is what
    gates real cancellation to the phase where it is genuinely effective:
    once audio frames are actively being published, cancelling stops further
    frame publication immediately and for real.
    """

    def __init__(
        self,
        *,
        room: "rtc.Room",
        session_id: str,
        case_id: str,
        on_shutdown: Callable[[str], None],
        job_id: str = "",
        room_id: str = "",
    ) -> None:
        self._room = room
        self.session_id = session_id
        self.case_id = case_id
        self._job_id = job_id
        self._room_id = room_id
        self._on_shutdown = on_shutdown
        self._shutdown_called = False  # idempotency guard - see _trigger_shutdown
        self._turn_lock = asyncio.Lock()
        self._session_factory = get_db_factory()
        self._audio_source: "rtc.AudioSource | None" = None
        self._started_at = time.monotonic()
        # The student's participant identity, once known - used to target
        # agent->browser control/status messages instead of blindly
        # broadcasting them (see _destination_identities). None (broadcast to
        # the whole room) is a safe fallback if somehow still unknown - there
        # is only ever one other participant in these rooms.
        self._student_identity: str | None = None
        # Idempotency/duplicate protection (Part 5): a clientTurnId currently
        # being processed, or already fully processed (success OR failure) -
        # NEVER for a turn dropped purely due to busy/barge-in (that one
        # never actually ran, so a later resend of the SAME id must still be
        # allowed to run for real). Bounded via _MAX_COMPLETED_TURN_IDS.
        self._in_flight_turn_ids: set[str] = set()
        self._completed_turn_ids: "OrderedDict[str, None]" = OrderedDict()
        # Phase D2: the CURRENTLY-RUNNING turn (set only once _turn_lock is
        # actually held - never for a busy-dropped turn, see
        # _handle_student_turn) and, separately, which clientTurnId (if any)
        # has genuinely reached the audio-publish phase - see the class
        # docstring for why interruption is gated on the LATTER, not just an
        # active task existing. Both are job-local instance attributes, never
        # shared across PocAgentSession instances/jobs (Phase D2 requirement:
        # no global registries).
        self._active_turn_task: "asyncio.Task[None] | None" = None
        self._active_client_turn_id: str | None = None
        self._speaking_client_turn_id: str | None = None

    def _log_agent_event(
        self, event: str, *, client_turn_id: str = "", elapsed_ms: float | None = None
    ) -> None:
        """Uniform structured line for every Part 8 telemetry event - always
        carries session_id/client_turn_id/job_id/room_id/elapsed_ms so a
        future incident can be grepped by ANY of those dimensions. Never logs
        patient text."""
        logger.info(
            "%s session_id=%s client_turn_id=%s job_id=%s room_id=%s elapsed_ms=%s",
            event, self.session_id, client_turn_id or "-", self._job_id, self._room_id,
            f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-",
        )

    def _destination_identities(self) -> list[str]:
        return [self._student_identity] if self._student_identity else []

    async def start(self) -> None:
        """Wire room event handlers, publish the ONE persistent audio track
        for this job's entire lifetime, verify the session actually exists,
        and only THEN announce readiness. The room is already connected
        (JobContext.connect() was awaited by the caller) - this method never
        connects/disconnects the room itself; that is the framework's job."""
        import livekit.rtc as rtc

        room = self._room

        # The student typically connects (creating the room, which triggers
        # our job dispatch) BEFORE this worker joins - so their identity is
        # usually already present here. participant_connected below is the
        # fallback for the less common ordering (e.g. after a reconnect).
        for identity in room.remote_participants:
            self._student_identity = identity
            break

        @room.on("data_received")
        def _on_data(packet: "rtc.DataPacket") -> None:
            if packet.topic == AGENT_CONTROL_TOPIC:
                self._handle_control_from_student(packet)
                return
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
                logger.warning(
                    "livekit_agent_bad_payload session_id=%s reason=missing_field", self.session_id,
                )
                return

            self._log_agent_event("livekit_agent_student_packet_received", client_turn_id=client_turn_id)

            # Turn ACK is the FIRST action for any structurally valid packet -
            # before any dedup/processing decision, before OpenAI/TTS ever
            # starts (Part 3). A duplicate gets ack'd again too (Part 5).
            self._send_turn_ack(client_turn_id)

            if client_turn_id in self._completed_turn_ids or client_turn_id in self._in_flight_turn_ids:
                self._log_agent_event("livekit_agent_duplicate_turn_received", client_turn_id=client_turn_id)
                return

            # Reserve the slot synchronously (not inside the coroutine below)
            # so a duplicate arriving in the brief window before the
            # scheduled task actually starts running is still caught.
            self._in_flight_turn_ids.add(client_turn_id)
            asyncio.ensure_future(self._handle_student_turn(text, client_turn_id))

        @room.on("participant_connected")
        def _on_participant_joined(participant: object) -> None:
            identity = getattr(participant, "identity", None)
            if identity and identity != AGENT_PARTICIPANT_IDENTITY:
                self._student_identity = identity

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
        logger.info("livekit_agent_track_published session_id=%s job_id=%s", self.session_id, self._job_id)

        loop = asyncio.get_running_loop()
        session_exists = await loop.run_in_executor(None, self._verify_session_exists)
        if not session_exists:
            logger.error(
                "livekit_agent_session_not_found_at_start session_id=%s job_id=%s", self.session_id, self._job_id,
            )
            self._trigger_shutdown("session_not_found")
            return

        self._send_agent_ready()

    def _verify_session_exists(self) -> bool:
        db = self._session_factory()
        try:
            return SessionRepository(db).get(self.session_id) is not None
        finally:
            db.close()

    def _publish_control(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        try:
            asyncio.ensure_future(
                self._room.local_participant.publish_data(
                    data,
                    reliable=True,
                    topic=AGENT_CONTROL_TOPIC,
                    destination_identities=self._destination_identities(),
                )
            )
        except Exception:
            logger.exception(
                "livekit_agent_status_publish_failed session_id=%s payload_type=%s", self.session_id, payload.get("type"),
            )
            self._log_agent_event("livekit_agent_status_publish_failed")

    def _send_agent_ready(self) -> None:
        elapsed_ms = (time.monotonic() - self._started_at) * 1000
        self._log_agent_event("livekit_agent_ready_sent", elapsed_ms=elapsed_ms)
        self._publish_control({"type": "agent_ready"})

    def _send_turn_ack(self, client_turn_id: str) -> None:
        self._log_agent_event("livekit_agent_turn_ack_sent", client_turn_id=client_turn_id)
        self._publish_control({"type": "turn_ack", "clientTurnId": client_turn_id})

    def _handle_control_from_student(self, packet: "rtc.DataPacket") -> None:
        """The only browser->agent message on AGENT_CONTROL_TOPIC today
        (Phase D2) - everything else on this topic flows agent->browser
        (agent_ready/turn_ack). Malformed/unknown payloads are silently
        ignored, matching _on_data's own bad-payload discipline (never
        crashes the handler, never surfaces to the student)."""
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        if payload.get("type") == INTERRUPT_PATIENT_TYPE:
            self._on_interrupt_patient(str(payload.get("clientTurnId") or ""))

    def _on_interrupt_patient(self, client_turn_id: str) -> None:
        """Cancels the ACTIVE turn's task iff it is genuinely, currently
        publishing audio for exactly this clientTurnId (see the class
        docstring's THINKING-vs-SPEAKING rationale). Anything else - no id,
        no active task, a mismatched/stale id, a turn not yet speaking, or a
        turn already resolved - is a safe, idempotent no-op (Phase D2
        requirement: double interrupt and a stale/late interrupt for an
        old turn must never affect a newer one).

        Acknowledges PROMPTLY and explicitly here (patient_turn_status
        "interrupted") rather than relying solely on the cancelled task's own
        cleanup - cancellation takes at least one more event-loop turn to
        actually unwind through _run_turn/_handle_student_turn's finally
        blocks, and the browser must not be left waiting on that.
        """
        if not client_turn_id:
            return
        if client_turn_id in self._completed_turn_ids:
            # Already resolved (naturally finished, failed, or a previous
            # interrupt already applied) - a duplicate/late resend is a
            # no-op, never a second cancellation or a second status message.
            self._log_agent_event("livekit_agent_interrupt_stale", client_turn_id=client_turn_id)
            return
        task = self._active_turn_task
        if (
            task is None
            or task.done()
            or self._active_client_turn_id != client_turn_id
            or self._speaking_client_turn_id != client_turn_id
        ):
            self._log_agent_event("livekit_agent_interrupt_stale", client_turn_id=client_turn_id)
            return

        self._log_agent_event("livekit_agent_interrupt_received", client_turn_id=client_turn_id)
        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                logger.exception("livekit_agent_clear_queue_failed session_id=%s", self.session_id)
        # Marked completed BEFORE cancelling: makes a second, near-simultaneous
        # interrupt_patient for the SAME clientTurnId (or a resend of it)
        # hit the _completed_turn_ids check above and no-op, without having
        # to wait for the cancellation to actually propagate first.
        self._mark_turn_completed(client_turn_id)
        task.cancel()
        self._send_turn_status(client_turn_id, "interrupted")
        self._log_agent_event("livekit_agent_interrupt_applied", client_turn_id=client_turn_id)

    def _mark_turn_completed(self, client_turn_id: str) -> None:
        """Records a clientTurnId as fully processed (success OR failure) so
        a LATER duplicate (e.g. a browser-side ack-timeout retry that arrives
        after processing already finished) is ack'd but never reprocessed.
        Deliberately NOT called for a turn dropped purely due to busy/
        barge-in - that one never actually ran, so it must remain eligible to
        run for real on a later resend. Bounded (oldest evicted first) so a
        very long interview cannot grow this unboundedly."""
        self._completed_turn_ids[client_turn_id] = None
        self._completed_turn_ids.move_to_end(client_turn_id)
        while len(self._completed_turn_ids) > _MAX_COMPLETED_TURN_IDS:
            self._completed_turn_ids.popitem(last=False)

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
        try:
            if self._turn_lock.locked():
                logger.info(
                    "livekit_agent_turn_dropped_busy session_id=%s client_turn_id=%s", self.session_id, client_turn_id,
                )
                # Never actually ran - do NOT mark completed, so a later
                # resend of this SAME clientTurnId (once the agent is free)
                # is still allowed to process for real.
                return
            async with self._turn_lock:
                # Phase D2: recorded only once the lock is actually held (a
                # busy-dropped turn above never reaches here) - asyncio.
                # current_task() IS the Task asyncio.ensure_future() created
                # for this coroutine in _on_data, so cancelling it here is
                # exactly what _on_interrupt_patient's task.cancel() acts on.
                self._active_turn_task = asyncio.current_task()
                self._active_client_turn_id = client_turn_id
                try:
                    await self._run_turn(text, client_turn_id)
                finally:
                    # Only clear if still ours - guards against a later,
                    # already-started turn's bookkeeping being wiped out by a
                    # STILL-unwinding earlier turn's finally block (shouldn't
                    # overlap given the lock, but cheap and exactly mirrors
                    # the same discipline already used for
                    # _speaking_client_turn_id/_completed_turn_ids).
                    if self._active_client_turn_id == client_turn_id:
                        self._active_turn_task = None
                        self._active_client_turn_id = None
        finally:
            self._in_flight_turn_ids.discard(client_turn_id)

    async def _run_turn(self, text: str, client_turn_id: str) -> None:
        loop = asyncio.get_running_loop()
        # Best-effort per-turn latency breakdown for real-device validation -
        # stage name -> monotonic timestamp, logged as ONE line at the end.
        # Never includes patient text, audio bytes, or any secret.
        stages: list[tuple[str, float]] = [("turn_received", time.monotonic())]

        def on_stage(name: str) -> None:
            stages.append((name, time.monotonic()))

        self._log_agent_event("livekit_agent_turn_processing_started", client_turn_id=client_turn_id)

        # Wraps BOTH OpenAI generation AND ElevenLabs/TTS generation (Part 7)
        # - no exception from EITHER stage, nor from publishing the resulting
        # audio, is allowed to escape this coroutine as an untracked asyncio
        # "Task exception was never retrieved" warning. Every path below
        # always sends an explicit patient_turn_status before returning.
        try:
            result = await loop.run_in_executor(
                None, self._generate_turn_sync, text, client_turn_id, on_stage
            )
            on_stage("persisted")

            pcm = await loop.run_in_executor(
                None,
                lambda: patient_adapter.synthesize_patient_audio_pcm(
                    case_id=self.case_id, text=result.patient_text, on_stage=on_stage
                ),
            )
            if pcm is None:
                # Deliberately NOT falling back to any other TTS here - the
                # student must see a real failure, not silently degrade to
                # legacy browser TTS (see the module docstring's "no browser
                # TTS fallback" requirement).
                logger.error("livekit_agent_tts_failed session_id=%s client_turn_id=%s", self.session_id, client_turn_id)
                self._send_turn_status(client_turn_id, "failed")
                self._log_turn_timing(client_turn_id, stages)
                return

            # A continuously-open WebRTC track has no natural "clip ended"
            # event the way a file-backed <audio> element does - the frontend
            # cannot reliably infer turn boundaries from element events
            # alone. Signal them explicitly via the data channel so the
            # frontend's state machine (THINKING -> SPEAKING -> LISTENING)
            # has an unambiguous source of truth.
            on_stage("first_audio_publish_start")
            self._send_turn_status(client_turn_id, "speaking_started")
            # Phase D2: marks the ONLY window in which _on_interrupt_patient
            # will actually cancel this task (see the class docstring) - set
            # synchronously here, independent of whether the fire-and-forget
            # "speaking_started" publish above has actually gone out yet.
            self._speaking_client_turn_id = client_turn_id
            await self._publish_pcm(pcm)
            on_stage("speech_complete")
            self._send_turn_status(client_turn_id, "speaking_ended")
            self._log_turn_timing(client_turn_id, stages)
            logger.info(
                "livekit_agent_turn_audio_published session_id=%s client_turn_id=%s bytes=%d",
                self.session_id, client_turn_id, len(pcm),
            )
        except asyncio.CancelledError:
            # Phase D2: an intentional interrupt (see _on_interrupt_patient,
            # the ONLY caller that ever cancels this task). The "interrupted"
            # patient_turn_status was already sent there, promptly, rather
            # than here - this branch exists so cancellation cleanly logs and
            # unwinds (releasing _turn_lock via the enclosing `async with`,
            # then _handle_student_turn's finally) WITHOUT falling through to
            # the generic `except Exception` below and wrongly emitting a
            # "failed" status for what was actually a deliberate interrupt.
            # Re-raised so the Task itself still completes as genuinely
            # cancelled (never swallowed) - required for _turn_lock release
            # and for asyncio's own bookkeeping to stay correct (this is what
            # avoids a stray "Task exception was never retrieved" warning).
            elapsed_ms = (time.monotonic() - stages[0][1]) * 1000
            self._log_agent_event(
                "livekit_agent_turn_interrupted", client_turn_id=client_turn_id, elapsed_ms=elapsed_ms,
            )
            raise
        except patient_adapter.LiveKitPocSessionNotFoundError:
            logger.error("livekit_agent_session_not_found session_id=%s client_turn_id=%s", self.session_id, client_turn_id)
            self._send_turn_status(client_turn_id, "failed")
        except Exception:
            elapsed_ms = (time.monotonic() - stages[0][1]) * 1000
            logger.exception(
                "livekit_agent_generation_failed session_id=%s client_turn_id=%s job_id=%s room_id=%s",
                self.session_id, client_turn_id, self._job_id, self._room_id,
            )
            self._log_agent_event(
                "livekit_agent_turn_processing_failed", client_turn_id=client_turn_id, elapsed_ms=elapsed_ms,
            )
            self._send_turn_status(client_turn_id, "failed")
        finally:
            # Phase D2: cleared here (not just on the natural speaking_ended
            # path above) so EVERY exit from this method - success, failure,
            # or interrupt-cancellation - leaves no stale speaking-phase
            # marker behind for a clientTurnId that is no longer actually
            # speaking.
            if self._speaking_client_turn_id == client_turn_id:
                self._speaking_client_turn_id = None
            # Marked completed (success, failure, OR interrupt) regardless of
            # which branch above ran, EXCEPT when this method wasn't entered
            # at all (busy-drop, handled in _handle_student_turn) - a
            # duplicate of this clientTurnId must never trigger a second
            # OpenAI/TTS call again after this point. Idempotent if
            # _on_interrupt_patient already called this for the same id.
            self._mark_turn_completed(client_turn_id)

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
                self._room.local_participant.publish_data(
                    payload,
                    reliable=True,
                    topic=PATIENT_TURN_STATUS_TOPIC,
                    destination_identities=self._destination_identities(),
                )
            )
        except Exception:
            logger.exception(
                "livekit_agent_status_publish_failed session_id=%s client_turn_id=%s status=%s",
                self.session_id, client_turn_id, status,
            )
            self._log_agent_event("livekit_agent_status_publish_failed", client_turn_id=client_turn_id)

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
    docstring's isolation notes) - this is also what makes running many
    copies of this worker process, on one machine or many, safe."""
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
        room=ctx.room, session_id=session_id, case_id=case_id,
        job_id=ctx.job.id, room_id=ctx.room.name, on_shutdown=_on_session_shutdown,
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
        # endpoint uses). This SAME agent_name is also the entire mechanism
        # LiveKit Cloud uses to load-balance jobs across multiple identical
        # worker processes - see the module docstring's "Horizontal scaling"
        # section; nothing else needs to change to run more than one.
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
