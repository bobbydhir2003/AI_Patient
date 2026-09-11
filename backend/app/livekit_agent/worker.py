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
import os
import socket
import time
from collections import OrderedDict
from enum import Enum
from typing import TYPE_CHECKING, Callable

from livekit.agents import AutoSubscribe, JobContext, JobRequest, WorkerOptions, cli

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.livekit_agent.realtime_client import REALTIME_PCM_SAMPLE_RATE
from app.repositories.session_repository import SessionRepository

if TYPE_CHECKING:
    import livekit.rtc as rtc
    from app.livekit_agent.realtime_session import RealtimeSession

logger = get_logger("app.livekit_agent.worker")

# --- Worker identity (horizontal-scaling observability) -------------------
# Constant for the life of the process. Lets logs show WHICH machine received a
# given interview, so LiveKit Cloud's distribution across Worker A/B/C is
# verifiable from logs alone. Never affects dispatch/isolation/routing, and
# never carries a secret, prompt ID, or student PII.
_WORKER_HOST = socket.gethostname()


def _worker_instance_id() -> str:
    """Human label for THIS worker machine: explicit LIVEKIT_WORKER_INSTANCE_ID
    if set, else the hostname. Log-only."""
    try:
        configured = (get_settings().livekit_worker_instance_id or "").strip()
    except Exception:
        configured = ""
    return configured or _WORKER_HOST


def _worker_ident() -> str:
    """Compact `worker_host=.. worker_pid=.. worker_instance=..` fragment for
    structured log lines. PID is read live (each job runs in its OWN spawned
    subprocess, so this is the job subprocess's PID)."""
    return f"worker_host={_WORKER_HOST} worker_pid={os.getpid()} worker_instance={_worker_instance_id()}"


STUDENT_TEXT_TOPIC = "student_text"
PATIENT_TURN_STATUS_TOPIC = "patient_turn_status"
# Control-plane messages (agent_ready, turn_ack) - distinct from
# patient_turn_status (turn/audio lifecycle) so the two concerns evolve
# independently. Both use topic + a `type`/`status` discriminator, matching
# the frontend's livekitPocEngine.ts AgentControlPayload/TurnStatusPayload.
AGENT_CONTROL_TOPIC = "agent_control"

# Phase G (Realtime engine only): agent->browser transcript-sync events so the
# visible conversation reflects the Realtime engine promptly, while the DB stays
# authoritative. Distinct topic from patient_turn_status (audio lifecycle) and
# agent_control (readiness/acks) so it evolves independently and a legacy
# frontend that never subscribes is completely unaffected. Every event carries
# clientTurnId + generation `epoch` (+ patientTurnId where applicable) so the
# frontend can drop a stale/out-of-order event. Event `type`s:
#   student_transcript  - the authoritative student text for this Realtime turn
#   patient_text_ready  - the backend-APPROVED patient text, sent BEFORE speech
#                         completes so it can render immediately
#   patient_text_final  - the reconciled final content (full on normal
#                         completion, the delivered PORTION after an interruption)
TRANSCRIPT_SYNC_TOPIC = "transcript_sync"

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

# 20ms frames - a conventional WebRTC frame duration.
_FRAME_SECONDS = 0.02

# Outbound patient-voice AudioSource playout buffer for prompt_agent. LiveKit's
# default is 1000ms, which stands ~1s of patient audio ahead of the student and
# is exactly what has to be discarded on barge-in - the main source of the
# "patient keeps talking" lag vs. the OpenAI Playground. 200ms is small enough
# to make interruption feel immediate while still absorbing normal jitter in
# OpenAI's audio-delta delivery (a handful of 20ms frames).
_PROMPT_AGENT_AUDIO_QUEUE_MS = 200

# Bounds memory for the per-session completed-clientTurnId dedup set (see
# PocAgentSession._mark_turn_completed) - a typical interview has a few dozen
# turns at most, so this is a generous cap, not a tuned limit.
_MAX_COMPLETED_TURN_IDS = 200

# Phase 1 (raw-audio ingestion, parallel to and NEVER driving the existing
# student_text conversation path): minimum spacing between aggregated
# "student_audio_ingest_active" log lines for one track, so a continuously
# active mic does not spam the log at frame rate (~50 frames/sec at 20ms
# frames) - see PocAgentSession._ingest_student_audio.
_STUDENT_AUDIO_LOG_INTERVAL_SECONDS = 10.0

# Must resolve before the frontend's existing 20-second agent_ready watchdog.
# This bounds only provider configuration acknowledgement; it does not change
# VAD, turn, generation, or response latency behavior.
_REALTIME_READY_TIMEOUT_SECONDS = 15.0

class TurnSource(str, Enum):
    """Turn-origin label, used purely for logging/observability: a browser
    SpeechRecognition final is always non-authoritative under prompt_agent
    (the Realtime audio path owns every spoken turn) - see _on_data."""

    BROWSER_TEXT = "browser_text"


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
    frontend's own single-active-generation guard). A fresh instance is
    constructed per job (see entrypoint()) - no state here is ever shared
    across interviews.

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
        worker_id: str = "",
    ) -> None:
        self._room = room
        self.session_id = session_id
        self.case_id = case_id
        self._job_id = job_id
        # LiveKit Cloud's framework worker id (log-only, for distribution
        # visibility across Worker A/B/C). Optional so existing tests that
        # construct PocAgentSession without it are unaffected.
        self._worker_id = worker_id
        self._room_id = room_id
        self._on_shutdown = on_shutdown
        self._shutdown_called = False  # idempotency guard - see _trigger_shutdown
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._accepting_audio_producers = True
        self._shutdown_task: "asyncio.Task[None] | None" = None
        self._turn_lock = asyncio.Lock()
        self._session_factory = get_db_factory()
        self._audio_source: "rtc.AudioSource | None" = None
        # prompt_agent's native voice is 24kHz - see start(), which publishes
        # the outbound track at this rate and is the only writer after init.
        self._patient_audio_sample_rate = REALTIME_PCM_SAMPLE_RATE
        # Phase 1 persistent ownership: the worker job owns this provider
        # session. Microphone AudioStreams are replaceable producers only and
        # must never close or clear it.
        self._realtime_session: "RealtimeSession | None" = None
        self._realtime_engine_active = False
        self._realtime_prompt_agent_active = False
        self._prompt_agent_runtime: "Any | None" = None
        self._realtime_session_started = asyncio.Event()
        self._realtime_configured_ready = False
        self._realtime_ready_task: "asyncio.Task[None] | None" = None
        self._realtime_producer_generation = 0
        self._realtime_producer_track_sid: str | None = None
        self._realtime_producer_attached = False
        self._agent_ready_sent = False
        # Orthogonal raw-audio candidate state, set/cleared by teardown paths
        # (_release_realtime_speech_waiters) - the audio-driven speech_started/
        # stopped signal itself is prompt_agent-native now (_on_prompt_speech_
        # started), so this Event stays permanently set outside of teardown.
        self._realtime_student_speech_active = False
        self._realtime_student_speech_stopped = asyncio.Event()
        self._realtime_student_speech_stopped.set()
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
        # Phase 5A (Step 13, diagnostic-only): the CURRENTLY-speaking
        # patient turn's own generated text, set/cleared in the SAME places
        # as _speaking_client_turn_id (see _run_turn) - used ONLY to log a
        # possible-echo signal (semantic_barge_in_possible_echo) when a
        # barge-in candidate transcript overlaps heavily with the patient's
        # own words. Never suppresses/blocks a real interruption - see
        # _on_semantic_barge_in.
        self._speaking_patient_text: str | None = None
        # Phase 1 raw-audio ingestion (parallel path, never drives the
        # conversation - see module docstring's Phase 1 note): one background
        # ingest task per subscribed STUDENT microphone track, keyed by track
        # SID so a repeated "track_subscribed" event for the SAME publication
        # (observed possible around reconnects) can never start a second,
        # overlapping ingest task for the same audio stream.
        self._student_audio_tasks: dict[str, "asyncio.Task[None]"] = {}

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
        # Decide ownership mode before registering/backfilling track handlers:
        # a pre-existing microphone may subscribe synchronously and its
        # producer must wait for the job-owned RealtimeSession, never start the
        # legacy pipeline by accident.
        startup_settings = get_settings()
        self._realtime_engine_active = startup_settings.realtime_engine_active
        self._realtime_prompt_agent_active = startup_settings.realtime_prompt_agent_active

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
            # Distinguishes a deliberate typed Send ("manual_typed") from a
            # browser SpeechRecognition final ("speech_browser" - also the
            # default for any older frontend build that never sends this
            # field at all).
            source = str(payload.get("source") or "speech_browser")
            is_manual_override = source == "manual_typed"

            self._log_agent_event("livekit_agent_student_packet_received", client_turn_id=client_turn_id)
            logger.info(
                "livekit_agent_student_packet_received session_id=%s client_turn_id=%s source=%s",
                self.session_id, client_turn_id, source,
            )

            # The raw student-AUDIO path (OpenAI Realtime prompt_agent) already
            # drives every spoken turn natively. A browser SpeechRecognition
            # final must never also trigger a turn - that would double-respond.
            # A deliberate MANUAL typed Send is a conscious student action and
            # IS honored, routed into the SAME Realtime conversation.
            browser_speech_ignored = not is_manual_override

            # Turn ACK is the FIRST action for any structurally valid packet -
            # before any dedup/processing decision, before Realtime is ever
            # touched. A duplicate gets ack'd again too. This ack only ever
            # tells the browser "the agent received your publish", never (by
            # itself) "this will drive the conversation" - the additive
            # `semanticIgnored` flag tells the frontend the difference, so
            # acking a non-authoritative browser packet cannot cause a
            # duplicate patient response; it only prevents a pointless
            # client-side delivery-retry storm for a packet the server is
            # about to ignore.
            self._send_turn_ack(client_turn_id, semantic_ignored=browser_speech_ignored)

            if browser_speech_ignored:
                # Browser speech-recognition text - ack'd (so the browser
                # doesn't retry) but never processed: the Realtime audio path
                # owns this spoken turn.
                self._log_agent_event(
                    "realtime_browser_text_ignored", client_turn_id=client_turn_id,
                )
                logger.info(
                    "realtime_browser_text_ignored session_id=%s client_turn_id=%s turn_source=%s",
                    self.session_id, client_turn_id, TurnSource.BROWSER_TEXT.value,
                )
                return

            if client_turn_id in self._completed_turn_ids or client_turn_id in self._in_flight_turn_ids:
                self._log_agent_event("livekit_agent_duplicate_turn_received", client_turn_id=client_turn_id)
                return

            # A MANUAL typed Send is honored through the SAME prompt_agent
            # Realtime conversation microphone turns use. Reserve the slot
            # synchronously before scheduling, just like an accepted spoken
            # turn, so a duplicate arriving before the task actually runs is
            # still caught by the check above.
            self._log_agent_event(
                "prompt_agent_manual_override_accepted", client_turn_id=client_turn_id,
            )
            self._in_flight_turn_ids.add(client_turn_id)
            asyncio.ensure_future(self._submit_prompt_agent_typed_text(client_turn_id, text))

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
            self._stop_all_student_audio_ingest(reason="participant_disconnected")
            self._trigger_shutdown("student_left")

        # --- Phase 1: raw student microphone audio ingestion (parallel path)
        # -----------------------------------------------------------------
        # The room-level SUBSCRIBE_NONE default (see entrypoint()) is left
        # untouched - the existing student_text/OpenAI/ElevenLabs path never
        # needed remote audio and still doesn't. These three handlers ONLY
        # ever selectively subscribe to, and ingest, the STUDENT's own
        # microphone track (never our own outbound "patient-voice" track,
        # which is a LOCAL track and never delivered through these REMOTE
        # events anyway, and never any camera/screen-share track) - see
        # _maybe_subscribe_student_audio's filters. Any error anywhere in
        # this path is caught locally and only ever logged - it must never
        # raise into room-level event dispatch or affect the conversation
        # path above.
        @room.on("track_published")
        def _on_track_published(
            publication: "rtc.RemoteTrackPublication", participant: "rtc.RemoteParticipant"
        ) -> None:
            try:
                self._maybe_subscribe_student_audio(publication, participant)
            except Exception:
                logger.exception("student_audio_track_published_handler_failed session_id=%s", self.session_id)

        @room.on("track_subscribed")
        def _on_track_subscribed(
            track: "rtc.Track",
            publication: "rtc.RemoteTrackPublication",
            participant: "rtc.RemoteParticipant",
        ) -> None:
            try:
                self._start_student_audio_ingest(track, publication, participant)
            except Exception:
                logger.exception("student_audio_track_subscribed_handler_failed session_id=%s", self.session_id)

        @room.on("track_unsubscribed")
        def _on_track_unsubscribed(
            track: "rtc.Track",
            publication: "rtc.RemoteTrackPublication",
            participant: "rtc.RemoteParticipant",
        ) -> None:
            self._stop_student_audio_ingest(publication.sid, reason="track_unsubscribed")

        # Backfill: the student's mic may already be published (and the
        # student may already be an existing remote participant we saw
        # above) before these handlers were just registered - mirrors the
        # SAME pre-existing-participant race this module already documents
        # for _student_identity a few lines up. Safe/idempotent even if
        # "track_published" ALSO fires for one of these afterwards -
        # _maybe_subscribe_student_audio no-ops once publication.subscribed
        # is already true.
        for existing_participant in room.remote_participants.values():
            # getattr, not direct attribute access: some room test doubles
            # (and, defensively, any future SDK shape) may not model
            # track_publications on the participant object itself - this
            # backfill loop must never raise into start(), which would also
            # tear down the existing student_text/agent_ready handshake.
            existing_publications = getattr(existing_participant, "track_publications", None) or {}
            for existing_publication in existing_publications.values():
                try:
                    self._maybe_subscribe_student_audio(existing_publication, existing_participant)
                except Exception:
                    logger.exception("student_audio_backfill_subscribe_failed session_id=%s", self.session_id)

        # prompt_agent's native voice is 24kHz - the only engine now. If the
        # Realtime engine turns out to be unavailable, start() shuts the job
        # down right after this (see the fail-closed check below); the track
        # published here never carries real audio in that case.
        self._patient_audio_sample_rate = REALTIME_PCM_SAMPLE_RATE
        # prompt_agent uses a small playout buffer so barge-in is not delayed
        # by ~1s of queued patient audio.
        audio_source_kwargs = {
            "sample_rate": self._patient_audio_sample_rate, "num_channels": 1,
        }
        if self._realtime_prompt_agent_active:
            audio_source_kwargs["queue_size_ms"] = _PROMPT_AGENT_AUDIO_QUEUE_MS
        self._audio_source = rtc.AudioSource(**audio_source_kwargs)
        track = rtc.LocalAudioTrack.create_audio_track("patient-voice", self._audio_source)

        loop = asyncio.get_running_loop()
        # P1 startup-latency: publish_track (an SFU round trip) and
        # verify_session_exists (a fast local DB query) do not depend on each
        # other, so launch both concurrently instead of strictly serializing
        # publish -> verify -> OpenAI as before. verify is still the
        # fail-closed GATE that must pass before an OpenAI Realtime session is
        # started at all - so a nonexistent session can never leave an orphan
        # provider connection (nothing has been started when verify fails).
        # This ONLY changes ordering/overlap; every readiness guarantee below
        # (and the "track published before agent_ready" invariant) is
        # preserved - see the awaited publish_task + ready-task arming order.
        publish_task = asyncio.ensure_future(
            room.local_participant.publish_track(track, rtc.TrackPublishOptions())
        )
        verify_task = asyncio.ensure_future(
            loop.run_in_executor(None, self._verify_session_exists)
        )

        try:
            session_exists = await verify_task
        except Exception:
            logger.exception(
                "livekit_agent_session_verify_failed session_id=%s job_id=%s",
                self.session_id, self._job_id,
            )
            session_exists = False
        if not session_exists:
            # Abort BEFORE any OpenAI Realtime session is started (we have not
            # called _maybe_start_realtime_session yet), so a bogus session can
            # never leave an orphan provider connection. Drain the in-flight
            # publish so it cannot outlive the job.
            await self._drain_publish_task(publish_task)
            logger.error(
                "livekit_agent_session_not_found_at_start session_id=%s job_id=%s", self.session_id, self._job_id,
            )
            await self._shutdown_and_signal("session_not_found")
            return

        # prompt_agent is the ONLY supported voice architecture now - the
        # legacy realtime-off ElevenLabs/patient_adapter path has been
        # removed. If the Realtime engine is unavailable (flag off or no
        # OPENAI_API_KEY), fail this job closed rather than silently running
        # the old pipeline or falling back to ElevenLabs.
        if not self._realtime_engine_active:
            await self._drain_publish_task(publish_task)
            logger.error(
                "livekit_agent_realtime_engine_unavailable session_id=%s job_id=%s "
                "reason=LIVEKIT_REALTIME_ENGINE_ENABLED_or_OPENAI_API_KEY_missing",
                self.session_id, self._job_id,
            )
            await self._shutdown_and_signal("realtime_engine_unavailable")
            return

        # Session verified + engine available: start the OpenAI Realtime session
        # NOW so its WebSocket connect + session.update round trip overlaps the
        # still-in-flight track publish above (previously that SFU round trip
        # ran entirely BEFORE this point). RealtimeSession.start() only launches
        # the background connect task and returns immediately, so this does not
        # itself block on the provider network.
        logger.info(
            "livekit_agent_realtime_start_kickoff session_id=%s job_id=%s elapsed_ms=%.0f",
            self.session_id, self._job_id, (time.monotonic() - self._started_at) * 1000,
        )
        realtime_session = await self._maybe_start_realtime_session(
            self._student_identity or "unattached", "unattached",
        )
        self._realtime_session_started.set()
        if realtime_session is None:
            await self._drain_publish_task(publish_task)
            await self._shutdown_and_signal("realtime_start_failed")
            return

        # The outbound patient-voice track MUST be published before agent_ready
        # is ever announced (a student must never be told "ready" before there
        # is a track to hear from). Awaiting it HERE - after the OpenAI connect
        # was kicked off, and BEFORE arming the ready task - both preserves that
        # invariant (the ready task is what sets _realtime_configured_ready, the
        # gate _maybe_send_realtime_agent_ready checks) and lets the publish
        # overlap the OpenAI connect rather than block it.
        try:
            await publish_task
        except Exception:
            logger.exception(
                "livekit_agent_track_publish_failed session_id=%s job_id=%s",
                self.session_id, self._job_id,
            )
            await self._shutdown_and_signal("track_publish_failed")
            return
        logger.info(
            "livekit_agent_track_published session_id=%s job_id=%s elapsed_ms=%.0f",
            self.session_id, self._job_id, (time.monotonic() - self._started_at) * 1000,
        )
        self._realtime_ready_task = asyncio.ensure_future(
            self._await_realtime_ready(realtime_session)
        )

    def _verify_session_exists(self) -> bool:
        db = self._session_factory()
        try:
            return SessionRepository(db).get(self.session_id) is not None
        finally:
            db.close()

    async def _drain_publish_task(self, publish_task: "asyncio.Task[None]") -> None:
        """Await an in-flight publish_track() task on an ABORT path, swallowing
        any error. The job is shutting down (session-not-found / engine
        unavailable / realtime start failed), so a publish failure here is moot
        and must never mask the real shutdown reason or leave a dangling task.
        Never raises."""
        try:
            await publish_task
        except Exception:
            logger.exception(
                "livekit_agent_track_publish_drain_failed session_id=%s job_id=%s",
                self.session_id, self._job_id,
            )

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
        if self._agent_ready_sent:
            return
        self._agent_ready_sent = True
        elapsed_ms = (time.monotonic() - self._started_at) * 1000
        self._log_agent_event("livekit_agent_ready_sent", elapsed_ms=elapsed_ms)
        # Distribution visibility: which worker machine actually voiced this
        # interview (host/pid/instance + framework worker_id). No PII/secrets.
        logger.info(
            "livekit_agent_realtime_ready session_id=%s job_id=%s worker_id=%s %s elapsed_ms=%.0f",
            self.session_id, self._job_id, self._worker_id, _worker_ident(), elapsed_ms,
        )
        # `semanticTurnControl` is always false now - the experimental
        # semantic-turn-control pipeline was removed; kept in the payload
        # shape only so an older frontend build parsing this field is
        # unaffected. `promptAgent` tells the frontend that OpenAI Realtime
        # OWNS speech detection, turn-taking AND transcription for this
        # session, so the browser must NOT run its own SpeechRecognition (it
        # would be redundant dead weight and cause UI flicker).
        self._publish_control({
            "type": "agent_ready",
            "semanticTurnControl": False,
            "promptAgent": self._realtime_prompt_agent_active,
        })

    def _maybe_send_realtime_agent_ready(self) -> None:
        """Compatibility signal, delayed until provider + mic path are real."""
        if (
            self._realtime_engine_active
            and self._realtime_configured_ready
            and self._realtime_producer_attached
            and self._realtime_session is not None
            and self._realtime_session.is_ready
            and not self._shutdown_called
        ):
            self._send_agent_ready()

    async def _await_realtime_ready(self, realtime_session: "RealtimeSession") -> None:
        ready = await realtime_session.wait_until_ready(_REALTIME_READY_TIMEOUT_SECONDS)
        if self._realtime_session is not realtime_session or self._shutdown_called:
            return
        if not ready:
            logger.error(
                "realtime_session_ready_timeout session_id=%s reason=%s",
                self.session_id, realtime_session.close_reason or "session_updated_timeout",
            )
            self._trigger_shutdown("realtime_not_ready")
            return
        self._realtime_configured_ready = True
        self._log_agent_event("realtime_session_configured_ready")
        self._maybe_send_realtime_agent_ready()

    def _on_realtime_unavailable(self, reason: str) -> None:
        self._realtime_configured_ready = False
        logger.error(
            "realtime_provider_unavailable session_id=%s reason=%s",
            self.session_id, reason,
        )
        self._trigger_shutdown("realtime_provider_unavailable")

    def _send_turn_ack(self, client_turn_id: str, *, semantic_ignored: bool = False) -> None:
        self._log_agent_event("livekit_agent_turn_ack_sent", client_turn_id=client_turn_id)
        logger.info(
            "livekit_agent_turn_ack_sent session_id=%s client_turn_id=%s semantic_ignored=%s",
            self.session_id, client_turn_id, semantic_ignored,
        )
        payload: dict = {"type": "turn_ack", "clientTurnId": client_turn_id}
        # Additive field, only ever present (and only ever true) when this ack
        # covers a browser-originated packet the agent will NOT process
        # because semantic control is authoritative for this session - see
        # the caller in _on_data. Omitted entirely for a normal ack (never
        # sent as `false`) so this never changes the payload shape any
        # existing frontend build already parses correctly.
        if semantic_ignored:
            payload["semanticIgnored"] = True
        self._publish_control(payload)

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

    def _cancel_active_patient_turn(self, client_turn_id: str, *, reason: str) -> bool:
        """Phase 5A (Step 12): the ONE cancellation primitive, shared by the
        manual interrupt_patient control message (_on_interrupt_patient) and
        semantic barge-in (_on_semantic_barge_in) - extracted verbatim from
        Phase D2's original _on_interrupt_patient body, behavior-identical.
        Returns True iff a genuinely active, currently-SPEAKING turn for
        this EXACT clientTurnId was cancelled; False for every already-
        resolved/stale/mismatched/not-yet-speaking case (a safe, idempotent
        no-op - never raises, never double-cancels). `reason` is log-only
        (e.g. "manual_interrupt" / "semantic_barge_in").

        Still SPEAKING-only (see the class docstring's THINKING-vs-SPEAKING
        rationale) - OpenAI/ElevenLabs run via loop.run_in_executor (a real
        OS thread) and are NOT genuinely cancellable, so a barge-in
        detected before _speaking_client_turn_id is set has nothing safe to
        cancel yet (Step 7.5: only act when a real handle exists)."""
        if not client_turn_id:
            return False
        if client_turn_id in self._completed_turn_ids:
            # Already resolved (naturally finished, failed, or a previous
            # interrupt/barge-in already applied) - a duplicate/late signal
            # is a no-op, never a second cancellation or status message.
            return False
        task = self._active_turn_task
        if (
            task is None
            or task.done()
            or self._active_client_turn_id != client_turn_id
            or self._speaking_client_turn_id != client_turn_id
        ):
            return False

        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                logger.exception("livekit_agent_clear_queue_failed session_id=%s", self.session_id)
        # Marked completed BEFORE cancelling: makes a second, near-simultaneous
        # cancellation signal for the SAME clientTurnId (manual or semantic,
        # or a resend of either) hit the _completed_turn_ids check above and
        # no-op, without having to wait for the cancellation to actually
        # propagate first (Step 10).
        self._mark_turn_completed(client_turn_id)
        task.cancel()
        self._send_turn_status(client_turn_id, "interrupted")
        logger.info(
            "livekit_agent_patient_turn_cancelled session_id=%s client_turn_id=%s reason=%s",
            self.session_id, client_turn_id, reason,
        )
        return True

    def _on_interrupt_patient(self, client_turn_id: str) -> None:
        """Cancels the ACTIVE turn's task iff it is genuinely, currently
        publishing audio for exactly this clientTurnId - see
        _cancel_active_patient_turn (Step 12: the shared primitive). Anything
        else - no id, no active task, a mismatched/stale id, a turn not yet
        speaking, or a turn already resolved - is a safe, idempotent no-op
        (Phase D2 requirement: double interrupt and a stale/late interrupt
        for an old turn must never affect a newer one).

        Acknowledges PROMPTLY and explicitly here (patient_turn_status
        "interrupted") rather than relying solely on the cancelled task's own
        cleanup - cancellation takes at least one more event-loop turn to
        actually unwind through _run_turn/_handle_student_turn's finally
        blocks, and the browser must not be left waiting on that.
        """
        if not client_turn_id:
            return
        # Logged unconditionally on receipt (unlike the pre-Phase-5A version,
        # which only logged this on the actionable path) - structured
        # telemetry now distinguishes "a message arrived" from "it was
        # actionable" the same way semantic_barge_in's own logs do; the one
        # existing test asserting on this log family
        # (test_interrupt_before_speaking_started_is_a_stale_noop) checks
        # for interrupt_stale via `any(...)`, unaffected by this extra line.
        self._log_agent_event("livekit_agent_interrupt_received", client_turn_id=client_turn_id)
        if not self._cancel_active_patient_turn(client_turn_id, reason="manual_interrupt"):
            self._log_agent_event("livekit_agent_interrupt_stale", client_turn_id=client_turn_id)
            return
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
        logger.info(
            "livekit_agent_job_shutdown session_id=%s job_id=%s worker_id=%s %s reason=%s",
            self.session_id, self._job_id, self._worker_id, _worker_ident(), reason,
        )
        self._accepting_audio_producers = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Defensive/test-only fallback: real LiveKit room callbacks execute
            # on the job loop, but keeping the method total preserves its
            # established synchronous callback contract for simple room fakes.
            asyncio.run(self._shutdown_and_signal(reason))
        else:
            self._shutdown_task = loop.create_task(self._shutdown_and_signal(reason))

    async def _shutdown_and_signal(self, reason: str) -> None:
        """Close job-owned resources before asking LiveKit to end the job."""
        self._shutdown_called = True
        await self.aclose(reason=reason)
        self._on_shutdown(reason)

    async def aclose(self, *, reason: str = "worker_shutdown") -> None:
        """Idempotent full worker-session teardown.

        Microphone producer cleanup is awaited first; the one job-owned
        RealtimeSession is then closed exactly once. AudioStream cleanup never
        reaches this method on its own.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._accepting_audio_producers = False
            self._release_realtime_speech_waiters(reason=reason)
            producer_tasks = list(self._student_audio_tasks.values())
            self._stop_all_student_audio_ingest(reason=reason)
            if producer_tasks:
                await asyncio.gather(*producer_tasks, return_exceptions=True)
            if self._realtime_ready_task is not None and not self._realtime_ready_task.done():
                if self._realtime_ready_task is not asyncio.current_task():
                    self._realtime_ready_task.cancel()
                    await asyncio.gather(self._realtime_ready_task, return_exceptions=True)
            realtime_session = self._realtime_session
            if realtime_session is not None:
                await realtime_session.cancel_active_response()
                await realtime_session.aclose()
                if self._realtime_session is realtime_session:
                    self._realtime_session = None
            prompt_runtime = self._prompt_agent_runtime
            if prompt_runtime is not None:
                await prompt_runtime.aclose()
            self._prompt_agent_runtime = None
            self._realtime_configured_ready = False
            self._realtime_producer_attached = False
            self._realtime_producer_track_sid = None
            logger.info(
                "livekit_agent_resources_closed session_id=%s reason=%s",
                self.session_id, reason,
            )

    # --- Phase 1: raw student microphone audio ingestion (parallel path,
    # never drives the conversation) -----------------------------------

    def _maybe_subscribe_student_audio(
        self, publication: "rtc.RemoteTrackPublication", participant: "rtc.RemoteParticipant"
    ) -> None:
        """Selectively subscribes to ONLY the student's microphone track -
        deliberately does NOT flip the room to SUBSCRIBE_ALL or subscribe to
        every track it sees. Filters out: our own agent identity (defensive;
        a local participant's publications are never delivered as "remote"
        here, but costs nothing to check - matches the same defensive style
        already used for AGENT_PARTICIPANT_IDENTITY elsewhere in this class),
        any non-audio track (camera/screen-share), and any audio track whose
        source isn't explicitly the microphone. Idempotent: calling this
        again for a publication already subscribed (e.g. once from the
        start()-time backfill loop and again from a live "track_published"
        event for the same publication) is a harmless no-op."""
        import livekit.rtc as rtc

        identity = getattr(participant, "identity", "")
        if not identity or identity == AGENT_PARTICIPANT_IDENTITY:
            return
        if publication.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if publication.source != rtc.TrackSource.SOURCE_MICROPHONE:
            return
        if publication.subscribed:
            return
        try:
            publication.set_subscribed(True)
        except Exception:
            logger.exception(
                "student_audio_subscribe_failed session_id=%s identity=%s track=%s",
                self.session_id, identity, publication.sid,
            )
            return
        logger.info(
            "student_audio_subscribe_requested session_id=%s identity=%s track=%s",
            self.session_id, identity, publication.sid,
        )

    def _start_student_audio_ingest(
        self,
        track: "rtc.Track",
        publication: "rtc.RemoteTrackPublication",
        participant: "rtc.RemoteParticipant",
    ) -> None:
        """Starts exactly ONE background ingest task per track SID. Guards
        against: (1) treating the agent's own outbound audio as student
        input - this handler only ever fires for REMOTE tracks in the first
        place, but the identity check mirrors _maybe_subscribe_student_audio
        defensively, since both guard the exact same invariant; (2) a
        duplicate/overlapping ingest task if "track_subscribed" ever fires
        twice for the same publication (e.g. a resubscribe) - a still-running
        task for the same SID is left alone rather than replaced."""
        import livekit.rtc as rtc

        identity = getattr(participant, "identity", "")
        if not identity or identity == AGENT_PARTICIPANT_IDENTITY:
            return
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if not self._accepting_audio_producers:
            return
        sid = publication.sid
        existing = self._student_audio_tasks.get(sid)
        if existing is not None and not existing.done():
            logger.info(
                "student_audio_duplicate_subscription_ignored session_id=%s identity=%s track=%s",
                self.session_id, identity, sid,
            )
            return
        producer_generation: int | None = None
        if self._realtime_engine_active:
            # Realtime accepts exactly one microphone producer. A replacement
            # invalidates the old producer synchronously before its task is
            # cancelled, so even one late frame cannot enter the persistent
            # provider session.
            if self._realtime_producer_track_sid == sid:
                return
            self._realtime_producer_generation += 1
            producer_generation = self._realtime_producer_generation
            old_sid = self._realtime_producer_track_sid
            self._realtime_producer_track_sid = sid
            self._realtime_producer_attached = False
            if old_sid is not None and old_sid != sid:
                self._stop_student_audio_ingest(old_sid, reason="realtime_producer_replaced")
        logger.info(
            "student_audio_track_subscribed session_id=%s identity=%s track=%s",
            self.session_id, identity, sid,
        )
        self._student_audio_tasks[sid] = asyncio.ensure_future(
            self._ingest_student_audio(
                track, identity, sid, producer_generation=producer_generation,
            )
        )

    def _stop_student_audio_ingest(self, track_sid: str, *, reason: str) -> None:
        """Cancels and forgets the ingest task for one track SID - a no-op if
        already stopped (e.g. "track_unsubscribed" firing after the ingest
        task already exited on its own, or firing twice)."""
        task = self._student_audio_tasks.pop(track_sid, None)
        if task is not None and not task.done():
            task.cancel()
        logger.info(
            "student_audio_track_unsubscribed session_id=%s track=%s reason=%s",
            self.session_id, track_sid, reason,
        )

    def _stop_all_student_audio_ingest(self, *, reason: str) -> None:
        """Called on participant-left and job-shutdown so no ingest task (and
        the AudioStream/FFI resources it holds) is ever left running past the
        life of the room - prevents leaking audio streams after disconnect."""
        self._release_realtime_speech_waiters(reason=reason)
        for sid in list(self._student_audio_tasks.keys()):
            self._stop_student_audio_ingest(sid, reason=reason)

    async def _ingest_student_audio(
        self,
        track: "rtc.Track",
        identity: str,
        track_sid: str,
        *,
        producer_generation: int | None = None,
    ) -> None:
        """Continuously consumes the student's microphone frames and forwards
        them to the job-owned Realtime session (started in start(), which
        fails the whole job closed before any track can be subscribed if the
        Realtime engine is unavailable - so `self._realtime_session` is
        always set by the time this runs). Also observes participant
        identity, track SID, sample rate, channel count, frame count, and
        elapsed audio duration - aggregated into one log line at most every
        _STUDENT_AUDIO_LOG_INTERVAL_SECONDS (never per-frame, never logging
        audio contents).

        Any exception anywhere in this path is caught and logged, never
        propagated - this whole path runs entirely in parallel with, and can
        never break, the existing student_text turn pipeline."""
        import livekit.rtc as rtc

        await self._realtime_session_started.wait()
        realtime_session = self._realtime_session
        if realtime_session is None:
            logger.error(
                "student_audio_realtime_unavailable session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            return
        stream = rtc.AudioStream(track, sample_rate=realtime_session.input_sample_rate, num_channels=1)
        if (
            producer_generation != self._realtime_producer_generation
            or track_sid != self._realtime_producer_track_sid
        ):
            await stream.aclose()
            return
        self._realtime_producer_attached = True
        self._maybe_send_realtime_agent_ready()
        frame_count = 0
        sample_rate = 0
        num_channels = 0
        total_duration_s = 0.0
        last_log_at = time.monotonic()
        try:
            async for event in stream:
                frame = event.frame
                frame_count += 1
                sample_rate = frame.sample_rate
                num_channels = frame.num_channels
                total_duration_s += frame.duration
                # Phase A: forward the SAME frame's raw PCM to the Realtime
                # session (listen-only - it never yet drives a patient turn).
                if realtime_session is not None:
                    if (
                        producer_generation != self._realtime_producer_generation
                        or track_sid != self._realtime_producer_track_sid
                        or self._realtime_session is not realtime_session
                    ):
                        break
                    realtime_session.push_audio_bytes(bytes(frame.data))
                now = time.monotonic()
                if now - last_log_at >= _STUDENT_AUDIO_LOG_INTERVAL_SECONDS:
                    last_log_at = now
                    logger.info(
                        "student_audio_ingest_active session_id=%s identity=%s track=%s "
                        "frames=%d sample_rate=%d channels=%d elapsed=%.1fs",
                        self.session_id, identity, track_sid, frame_count, sample_rate, num_channels, total_duration_s,
                    )
        except asyncio.CancelledError:
            # Cleanup (finally, below) still runs before this propagates -
            # same discipline as _run_turn's own CancelledError handling.
            raise
        except Exception:
            logger.exception(
                "student_audio_ingest_error session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
        finally:
            logger.info(
                "student_audio_ingest_summary session_id=%s identity=%s track=%s "
                "frames=%d sample_rate=%d channels=%d elapsed=%.1fs",
                self.session_id, identity, track_sid, frame_count, sample_rate, num_channels, total_duration_s,
            )
            if realtime_session is not None:
                if (
                    producer_generation == self._realtime_producer_generation
                    and track_sid == self._realtime_producer_track_sid
                ):
                    self._release_realtime_speech_waiters(reason="audio_ingest_stopped")
                    self._realtime_producer_attached = False
                    self._realtime_producer_track_sid = None
            try:
                await stream.aclose()
            except Exception:
                logger.exception(
                    "student_audio_stream_close_failed session_id=%s track=%s", self.session_id, track_sid,
                )
            if self._student_audio_tasks.get(track_sid) is asyncio.current_task():
                self._student_audio_tasks.pop(track_sid, None)

    async def _maybe_start_realtime_session(
        self, identity: str, track_sid: str
    ) -> "RealtimeSession | None":
        """OpenAI Realtime prompt_agent entry point (the only voice
        architecture now): returns a running RealtimeSession, or None if the
        engine is off/unconfigured/fails to start. The caller (start()) fails
        the whole job closed on None rather than falling back to any legacy
        path - see start()'s realtime-unavailable check. Never raises."""
        settings = get_settings()
        if not settings.realtime_engine_active:
            return None
        if self._realtime_session is not None:
            return self._realtime_session
        return await self._start_prompt_agent_session(settings, identity, track_sid)

    async def _start_prompt_agent_session(
        self, settings, identity: str, track_sid: str,
    ) -> "RealtimeSession | None":
        """prompt_agent path: resolve this interview's patient config from the
        trusted server-side case_id and open ONE Realtime session that OWNS the
        conversation. Fails safe to None (like the parent) if the patient is
        unconfigured or construction fails, so the caller can shut down cleanly
        rather than voice the wrong / an unconfigured patient."""
        from app.livekit_agent.realtime_client import OpenAIRealtimeClient
        from app.livekit_agent.realtime_patient_configs import (
            PatientConfigError,
            resolve_patient_config,
        )
        from app.livekit_agent.realtime_prompt_agent import PromptAgentRuntime
        from app.livekit_agent.realtime_session import RealtimeSession

        try:
            config = resolve_patient_config(self.case_id, settings)
        except PatientConfigError:
            logger.exception(
                "prompt_agent_config_unresolved session_id=%s case_id=%s",
                self.session_id, self.case_id,
            )
            return None
        realtime_session = None
        try:
            prompt_runtime = PromptAgentRuntime(
                session_id=self.session_id,
                case_id=self.case_id,
                config=config,
                db_factory=self._session_factory,
                on_audio=self._publish_realtime_pcm,
                on_student_final=self._on_prompt_student_final,
                on_patient_final=self._on_prompt_patient_final,
            )
            client = OpenAIRealtimeClient(api_key=settings.openai_api_key, model=config["model"])
            realtime_session = RealtimeSession(
                session_id=self.session_id, case_id=self.case_id, identity=identity,
                track_sid=track_sid, client=client, settings=settings,
                # Realtime owns turn-taking AND authoring: no backend turn
                # acceptance. speech_started only clears the local playback
                # buffer on barge-in (OpenAI cancels its own response).
                on_speech_started=self._on_prompt_speech_started,
                on_unavailable=self._on_realtime_unavailable,
                prompt_agent=prompt_runtime,
            )
            self._realtime_session = realtime_session
            self._prompt_agent_runtime = prompt_runtime
            # Launch the decoupled outbound-audio publisher so the Realtime
            # receive loop never blocks on LiveKit capture_frame back-pressure
            # (barge-in latency fix). The AudioSource already exists (created in
            # start() before ingest); the publisher just awaits its queue.
            prompt_runtime.start()
            await realtime_session.start()
        except Exception:
            if self._realtime_session is realtime_session:
                self._realtime_session = None
                self._prompt_agent_runtime = None
            logger.exception(
                "prompt_agent_session_start_failed session_id=%s case_id=%s identity=%s track=%s",
                self.session_id, self.case_id, identity, track_sid,
            )
            return None
        logger.info(
            "prompt_agent_engine_active session_id=%s case_id=%s identity=%s track=%s model=%s",
            self.session_id, self.case_id, identity, track_sid,
            config["model"],
        )
        return realtime_session

    def _on_prompt_speech_started(self) -> None:
        """Barge-in in prompt_agent mode: immediately drop any queued patient
        audio so a cancelled answer stops the instant the student speaks. OpenAI
        (interrupt_response=True) cancels its own in-flight response server-side;
        the runtime rejects any late audio deltas for it."""
        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                logger.exception(
                    "prompt_agent_audio_clear_failed session_id=%s", self.session_id,
                )

    def _on_prompt_student_final(
        self, client_turn_id: str, epoch: int, student_turn_id: str, text: str,
    ) -> None:
        self._send_transcript_sync(
            "student_transcript", client_turn_id, epoch=epoch, text=text,
            student_turn_id=student_turn_id,
        )

    def _on_prompt_patient_final(
        self, client_turn_id: str, epoch: int, patient_turn_id: str, text: str,
    ) -> None:
        self._send_transcript_sync(
            "patient_text_final", client_turn_id, epoch=epoch, text=text,
            patient_turn_id=patient_turn_id,
        )

    async def _submit_prompt_agent_typed_text(self, client_turn_id: str, text: str) -> None:
        """Typed-input entry point for prompt_agent: hands the text straight to
        the Realtime conversation the SAME way a spoken turn reaches it - no
        backend generation, no ElevenLabs. `_in_flight_turn_ids`/
        `_completed_turn_ids` bookkeeping mirrors the legacy busy/dedup
        contract so a resend of the same clientTurnId is still caught."""
        try:
            runtime = self._prompt_agent_runtime
            if runtime is None:
                logger.error(
                    "prompt_agent_typed_text_no_runtime session_id=%s client_turn_id=%s",
                    self.session_id, client_turn_id,
                )
                return
            await runtime.submit_typed_text(client_turn_id, text)
        finally:
            self._in_flight_turn_ids.discard(client_turn_id)
            self._mark_turn_completed(client_turn_id)

    def _release_realtime_speech_waiters(self, *, reason: str) -> None:
        """Lifecycle-safe release used whenever Realtime ingest is torn down."""
        was_active = self._realtime_student_speech_active
        self._realtime_student_speech_active = False
        self._realtime_student_speech_stopped.set()
        if was_active:
            logger.info(
                "realtime_candidate_speech_released session_id=%s reason=%s",
                self.session_id, reason,
            )

    async def _publish_realtime_pcm(self, pcm: bytes) -> None:
        """Phase D: publish one chunk of Realtime's 24kHz native-voice PCM to
        the outbound LiveKit AudioSource. Separate from the legacy _publish_pcm
        (which frames at 16kHz for ElevenLabs) so neither path can use the
        wrong rate - frames here are built at self._patient_audio_sample_rate,
        which start() set to 24kHz for the Realtime engine."""
        import livekit.rtc as rtc

        if self._audio_source is None or not pcm:
            return
        rate = self._patient_audio_sample_rate
        frame_bytes = int(rate * _FRAME_SECONDS) * 2
        for i in range(0, len(pcm), frame_bytes):
            chunk = pcm[i : i + frame_bytes]
            if len(chunk) < 2:
                break
            frame = rtc.AudioFrame(
                data=chunk, sample_rate=rate, num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await self._audio_source.capture_frame(frame)

    def _send_transcript_sync(
        self, event_type: str, client_turn_id: str, *, epoch: int, text: str,
        patient_turn_id: str | None = None, student_turn_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Phase G: publish one transcript-sync event (see TRANSCRIPT_SYNC_TOPIC)
        targeted at the student, carrying the generation epoch so the frontend
        can reject a stale/out-of-order event. Never raises - a failed publish
        here must never break turn processing (the DB row is already the
        authoritative record regardless)."""
        body: dict = {"type": event_type, "clientTurnId": client_turn_id, "epoch": epoch, "text": text}
        if patient_turn_id is not None:
            body["patientTurnId"] = patient_turn_id
        if student_turn_id is not None:
            body["studentTurnId"] = student_turn_id
        if reason is not None:
            body["reason"] = reason
        payload = json.dumps(body).encode("utf-8")
        try:
            asyncio.ensure_future(
                self._room.local_participant.publish_data(
                    payload, reliable=True, topic=TRANSCRIPT_SYNC_TOPIC,
                    destination_identities=self._destination_identities(),
                )
            )
        except Exception:
            logger.exception(
                "realtime_transcript_sync_publish_failed session_id=%s client_turn_id=%s event=%s",
                self.session_id, client_turn_id, event_type,
            )

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


async def _handle_job_request(request: JobRequest) -> None:
    """Accept every job dispatched to us under our fixed agent_name, joining
    with a FIXED, predictable identity (AGENT_PARTICIPANT_IDENTITY) rather
    than the framework's default "agent-<job_id>" - see that constant's
    docstring for why."""
    # Startup-latency marker: the moment LiveKit's job dispatch reached this
    # worker and we accepted it (the top of the worker-side critical path).
    # getattr: the real JobRequest exposes `.id`; a minimal test fake may not.
    logger.info(
        "livekit_agent_job_accepted job_id=%s %s monotonic_ms=%.0f",
        getattr(request, "id", "-"), _worker_ident(), time.monotonic() * 1000,
    )
    await request.accept(identity=AGENT_PARTICIPANT_IDENTITY, name="PT AI Patient")


async def entrypoint(ctx: JobContext) -> None:
    """One call per accepted job = one student interview. Everything here is
    scoped to THIS job - no module-level dict/cache keyed by session_id, no
    state that could leak between two concurrent interviews (see the module
    docstring's isolation notes) - this is also what makes running many
    copies of this worker process, on one machine or many, safe."""
    # Startup-latency marker: entrypoint invoked. Monotonic wall clock (ms) so
    # the worker-side critical path (job_accepted -> entrypoint -> job_connected
    # -> realtime_start_kickoff -> track_published -> agent_ready_sent) can be
    # reconstructed from logs alone. No secrets/PII - only ids and timings.
    entrypoint_started_at = time.monotonic()
    logger.info(
        "livekit_agent_entrypoint_started job_id=%s worker_id=%s %s monotonic_ms=%.0f",
        ctx.job.id, ctx.worker_id, _worker_ident(), entrypoint_started_at * 1000,
    )
    parsed = parse_job_metadata(ctx.job.metadata)
    if parsed is None:
        logger.error("livekit_agent_job_missing_metadata job_id=%s", ctx.job.id)
        ctx.shutdown(reason="missing_or_invalid_metadata")
        return
    session_id, case_id = parsed

    # SUBSCRIBE_NONE: the conversation itself still never depends on the
    # student's raw mic audio (transcription is client-side via the browser's
    # Web Speech API, arriving as a "student_text" data message) - blindly
    # SUBSCRIBE_ALL-ing here would still be waste. Phase 1 adds a PARALLEL,
    # non-driving raw-audio ingestion path on top of this same NONE default:
    # PocAgentSession selectively subscribes to (only) the student's
    # microphone track itself - see start()'s "track_published"/
    # "track_subscribed"/"track_unsubscribed" handlers and
    # _maybe_subscribe_student_audio.
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)
    logger.info(
        "livekit_agent_job_connected job_id=%s worker_id=%s %s session_id=%s case_id=%s room=%s elapsed_ms=%.0f",
        ctx.job.id, ctx.worker_id, _worker_ident(), session_id, case_id, ctx.room.name,
        (time.monotonic() - entrypoint_started_at) * 1000,
    )

    done = asyncio.Event()
    poc_session: PocAgentSession | None = None

    def _on_session_shutdown(reason: str) -> None:
        ctx.shutdown(reason=reason)
        done.set()

    async def _on_ctx_shutdown(reason: str) -> None:
        # Safety net: if the framework itself ends the job for a reason our
        # own participant_disconnected handler never saw (e.g. a drain/
        # timeout), close the job-owned Realtime session before entrypoint
        # returns.
        if poc_session is not None:
            await poc_session.aclose(reason=reason or "context_shutdown")
        done.set()

    ctx.add_shutdown_callback(_on_ctx_shutdown)

    poc_session = PocAgentSession(
        room=ctx.room, session_id=session_id, case_id=case_id,
        job_id=ctx.job.id, room_id=ctx.room.name, on_shutdown=_on_session_shutdown,
        worker_id=ctx.worker_id,
    )
    await poc_session.start()

    # Block here for the job's entire lifetime - returning from entrypoint()
    # ends the job, so this await is what keeps the interview's room/track
    # alive until the student leaves (or the framework shuts us down).
    await done.wait()
    await poc_session.aclose(reason="entrypoint_finished")


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
    # Capacity/observability visibility (requirement 6): log the EFFECTIVE
    # configured worker capacity once at startup - no new monitoring system,
    # just the values this process will advertise to LiveKit Cloud.
    logger.info(
        "livekit_worker_options agent_name=%s load_threshold=%.2f num_idle_processes=%d "
        "job_memory_warn_mb=%d job_memory_limit_mb=%d job_executor=PROCESS %s",
        settings.livekit_agent_name,
        settings.livekit_worker_load_threshold,
        settings.livekit_worker_num_idle_processes,
        settings.livekit_worker_job_memory_warn_mb,
        settings.livekit_worker_job_memory_limit_mb,
        _worker_ident(),
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
        # worker processes/machines - running N copies of this service with the
        # SAME agent_name is the whole horizontal-scaling story; nothing else
        # needs to change (see docs/DEPLOYMENT.md's multi-worker topology).
        agent_name=settings.livekit_agent_name,
        # Explicit, not the framework's own os.environ fallback - single
        # source of truth stays app.core.config.get_settings(), exactly like
        # every other provider credential in this codebase.
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        # Explicit worker capacity (requirement 2/3): plain values so they apply
        # in BOTH the `dev` and `start` CLI modes, instead of the framework's
        # dev/prod ServerEnvOption split (inf/0.7 for load, 0/4 for idle procs).
        # load_threshold: THIS worker stops taking new jobs above this CPU load
        # so LiveKit Cloud routes to a less-loaded peer sharing agent_name. This
        # is the intended "dispatch by availability/load" model - deliberately
        # NOT a fixed hard per-machine job cap (livekit-agents has no such safe
        # native option; load-based backpressure is the supported mechanism).
        load_threshold=settings.livekit_worker_load_threshold,
        num_idle_processes=settings.livekit_worker_num_idle_processes,
        job_memory_warn_mb=settings.livekit_worker_job_memory_warn_mb,
        job_memory_limit_mb=settings.livekit_worker_job_memory_limit_mb,
        # job_executor_type is intentionally left at the framework default
        # (JobExecutorType.PROCESS): per-job OS-process isolation is exactly the
        # property the module docstring's multi-interview isolation relies on.
    )


if __name__ == "__main__":
    # Persistent process - started ONCE by systemd (ptai-livekit-agent.service,
    # see docs/DEPLOYMENT.md), never per-interview, never with --room/
    # --session-id/--case-id. Run via:
    #   python -m app.livekit_agent.worker start
    cli.run_app(_build_worker_options())
