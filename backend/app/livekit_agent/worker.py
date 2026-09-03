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
import collections
import itertools
import json
import logging
import threading
import time
from collections import OrderedDict
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from livekit.agents import AutoSubscribe, JobContext, JobRequest, WorkerOptions, cli

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_client import REALTIME_PCM_SAMPLE_RATE
from app.livekit_agent.turn_detector import (
    BargeInDecision,
    SemanticTurnDetector,
    TurnContext,
    TurnDecision,
    classify_barge_in,
    normalize_barge_in_text,
)
from app.repositories.session_repository import SessionRepository

if TYPE_CHECKING:
    import numpy as np
    import livekit.rtc as rtc
    from livekit.agents import stt as agents_stt
    from livekit.agents import vad as agents_vad
    from app.livekit_agent.realtime_session import RealtimeSession

logger = get_logger("app.livekit_agent.worker")

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

# 20ms frames at 16kHz mono 16-bit PCM = 640 bytes/frame - a conventional
# WebRTC frame duration. Unchanged from Phase 1.
_FRAME_SECONDS = 0.02
_FRAME_BYTES = int(patient_adapter.LIVEKIT_PCM_SAMPLE_RATE * _FRAME_SECONDS) * 2

# Outbound patient-voice AudioSource playout buffer for prompt_agent. LiveKit's
# default is 1000ms, which stands ~1s of patient audio ahead of the student and
# is exactly what has to be discarded on barge-in - the main source of the
# "patient keeps talking" lag vs. the OpenAI Playground. 200ms is small enough
# to make interruption feel immediate while still absorbing normal jitter in
# OpenAI's audio-delta delivery (a handful of 20ms frames). Only prompt_agent
# uses this; legacy/controlled/native modes keep LiveKit's default.
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

# Phase 2 (server-side VAD/STT, PARALLEL/OBSERVATIONAL only - see
# _StudentVadSttPipeline below): the sample rate requested from both Silero
# VAD and Deepgram STT. Phase 3's SmartTurnDetector ALSO requires exactly
# this rate (see turn_detector.py's MODEL_SAMPLE_RATE) - _ingest_student_audio
# requests this rate directly from LiveKit's own AudioStream (native FFI
# resampling, done ONCE at the source), so none of the three consumers
# (VAD, STT, the candidate-turn audio buffer) ever has to resample the
# same frame itself.
_VAD_STT_SAMPLE_RATE = 16000

# Phase 3 (EXPERIMENTAL semantic turn detection - see
# _CandidateTurnCoordinator below): hard cap on the candidate-turn audio
# buffer's own memory footprint across a long multi-segment HOLD chain.
# SmartTurnDetector itself only ever looks at the trailing WINDOW_SECONDS
# (8s, see turn_detector.py) regardless - this is purely an upper bound so
# a candidate turn that HOLDs through several long pauses cannot grow this
# buffer unboundedly (Step 4's "no unlimited accumulation" requirement).
_MAX_CANDIDATE_AUDIO_SECONDS = 30.0

# Bounded wait, after VAD END_OF_SPEECH, for the matching Deepgram
# FINAL_TRANSCRIPT to arrive before evaluating the semantic detector anyway
# (Step 9's STT/VAD race) - short and deliberately NOT a general
# conversational delay: Deepgram's own finalization latency after
# endpointing is typically well under this, so in the common case this
# wait resolves almost immediately (an asyncio.Event, not a flat sleep).
_STT_FINAL_GRACE_SECONDS = 1.0

# Path to the vendored Smart Turn v3.2 ONNX model (see
# app/livekit_agent/models/NOTICE.md for provenance/license/why-vendored).
_SMART_TURN_MODEL_PATH = Path(__file__).parent / "models" / "smart_turn_v3_2_cpu.onnx"

# Phase 4 (EXPERIMENTAL turn CONTROL): consecutive _CandidateTurnCoordinator
# detector-evaluation failures (see _evaluate_boundary's except block) before
# a session with semantic control genuinely active gives up on the semantic
# pipeline and falls back to browser text control for the rest of the
# session (Step 11) - see PocAgentSession._fallback_to_browser_control. A
# SINGLE detector error is not fatal (Step 10: the candidate state is left
# exactly as-is and the next boundary evaluates normally); only a run of
# them indicates the pipeline itself (not just one inference call) is
# unhealthy.
_SEMANTIC_ERROR_FALLBACK_THRESHOLD = 3

# Phase 6 (EXPERIMENTAL patient backchanneling - Step 4): a HOLD-classified
# candidate transcript this short ("So...", "Um...") isn't "enough content
# for acknowledgement to make sense" - deliberately conservative (Step 6's
# own worked example: "Where does it hurt?" is 4 words and is a direct
# question anyway, so this alone doesn't fully distinguish HOLD-vs-END, but
# combined with only ever firing on genuine HOLD it's an effective, simple
# filter against backchanneling on a bare filler fragment).
_MIN_BACKCHANNEL_WORDS = 3

# Phase 6 (Step 5): delay AFTER a confident HOLD before a scheduled
# backchannel actually plays - cancelled immediately if the student resumes
# speaking first (see PocAgentSession._cancel_pending_backchannel). Chosen
# near the middle of the requested 700-1200ms range. Total effective delay
# from the student's actual last word of speech is this PLUS the existing
# Phase 2/3 pipeline: VAD's own min_silence_duration (~550ms) + the STT
# final-grace wait (up to _STT_FINAL_GRACE_SECONDS, usually far less in
# practice since Deepgram typically finalizes close to when VAD detects
# silence) + Smart Turn's own inference (~50-100ms) - roughly 1.65-2.5s
# typical end-to-end, up to ~2.65s worst case. A plain module constant, not
# a new env var - one well-documented number needs no config surface.
_BACKCHANNEL_HOLD_DELAY_SECONDS = 0.9

# Phase 6 (Step 13): how long AFTER a backchannel clip's audio genuinely
# starts publishing its exact-match echo guard stays armed - covers the
# clip's own playback duration (computed exactly from its PCM byte count)
# plus this extra grace window, since Deepgram's matching STT final for the
# patient's own "mm-hmm" can lag slightly behind the audio itself.
_BACKCHANNEL_ECHO_GRACE_SECONDS = 1.5

# Phase 6 (Step 9): a deliberately tiny, semantically-neutral set - never
# "yes"/"no"/"exactly"/"I understand" (those can accidentally communicate
# clinical facts or agreement - see the module's own Step 9 requirement).
# Step 9 also explicitly says not to add randomness until deterministic
# behavior is well-tested - v1 always uses element [0]; the other two exist
# so a future phase can randomize among them without any structural change
# here.
_BACKCHANNEL_PHRASES: tuple[str, ...] = ("Mm-hmm.", "Uh-huh.", "Okay.")

# Phase 7 (EXPERIMENTAL semantic resolution timers - see
# _CandidateTurnCoordinator._arm_pending_resolution): bounded, cancellable
# safety net for a false-HOLD (Smart Turn HOLDs a genuinely complete
# utterance, student then waits silently for an answer - today this stays
# open for the rest of the session, see the Phase 7 audit). Anchored to the
# triggering boundary's VAD END_OF_SPEECH timestamp (_vad_end_at), NOT to
# whenever Smart Turn happens to finish evaluating - so STT-grace-wait/
# inference latency already spent counts AGAINST this budget rather than
# stacking on top of it, keeping total worst-case latency from the
# student's actual last word bounded and predictable regardless of STT
# timing jitter. A plain module constant, starting value for experimental
# tuning - not derived, see the Phase 7 design doc's timing model.
_HOLD_RECOVERY_SECONDS = 2.3

# Phase 7: bounded, cancellable, UNIVERSAL grace after every Smart Turn END
# decision (no lexical/grammar heuristic - see the Phase 7 design doc for
# why a text-based classifier was rejected) before the candidate is reset
# and submitted - lets a brief natural pause ("When your pain started...
# were you walking or sitting?") rejoin the SAME semantic turn instead of
# splitting into two. Same VAD-END anchoring principle as
# _HOLD_RECOVERY_SECONDS above, though the effect size is smaller given how
# short this window is. Starting value for experimental tuning.
_PENDING_END_GRACE_SECONDS = 0.4

# Phase 7: the two kinds of pending semantic resolution a coordinator can
# have armed - see _CandidateTurnCoordinator._arm_pending_resolution. Never
# more than one armed at a time for a given coordinator (single-slot
# invariant - see _CandidateTurnCoordinator._pending_kind).
_HOLD_RECOVERY_KIND = "hold_recovery"
_PENDING_END_KIND = "pending_end"


class TurnSource(str, Enum):
    """Phase 4: explicit turn-origin concept (Step 3). Exactly one of these
    can ever drive a given patient turn for a session at a given time -
    BROWSER_TEXT while semantic control is off/not-yet-active/fallen-back,
    SERVER_SEMANTIC only while PocAgentSession._semantic_control_active is
    True. Used purely for logging/observability; _handle_student_turn's
    dedup/lock behavior is identical for both."""

    BROWSER_TEXT = "browser_text"
    SERVER_SEMANTIC = "server_semantic"
    # Phase 4 turn-ID sync fix: an explicit student Send (typed chat, never a
    # SpeechRecognition final) submitted while semantic control is active.
    # Deliberately bypasses the BROWSER_TEXT ignore-path below - the student
    # took a conscious action, so it is treated as an authoritative turn even
    # though semantic control governs spoken input for this session. Still
    # funnels through the exact same _handle_student_turn/_run_turn pipeline
    # and dedup/lock as every other turn source - see _on_data.
    MANUAL_OVERRIDE = "manual_override"


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


def _normalize_for_fidelity(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace - so the Phase D
    verbatim-fidelity check (see PocAgentSession._check_voice_fidelity) compares
    spoken vs approved CONTENT, not trivial STT punctuation/casing. No regex
    (keeps worker.py's import surface unchanged)."""
    # Drop apostrophes outright (not to a space) so contractions collapse
    # consistently ("I've" -> "ive"), then map remaining punctuation to spaces.
    stripped = (text or "").replace("'", "").replace("’", "")
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in stripped)
    return " ".join(cleaned.split())


class _StudentVadSttPipeline:
    """Phase 2: a PARALLEL, OBSERVATIONAL-only VAD+STT pipeline scoped to ONE
    student audio ingest task's lifetime (see
    PocAgentSession._ingest_student_audio, which owns and feeds this). Never
    referenced by, and has no way to reach, any turn-driving state (turn
    lock, dedup sets, DB writes, the student_text/patient_turn_status
    topics) - it only ever logs diagnostics. A failure ANYWHERE inside this
    class - VAD inference, the Deepgram websocket, a malformed event - is
    caught and logged right here; it can never raise into, cancel, or
    otherwise affect the raw-audio ingest task that owns it, let alone the
    conversation path.

    Both frame consumers are fed the SAME rtc.AudioFrame from the ingest
    task's single rtc.AudioStream - each resamples internally only if
    needed (see _VAD_STT_SAMPLE_RATE's comment), so the audio is decoded
    once regardless of how many of these two consumers are active.
    """

    def __init__(
        self,
        *,
        session_id: str,
        identity: str,
        track_sid: str,
        vad: "agents_vad.VAD",
        stt: "agents_stt.STT",
        candidate_turn: "_CandidateTurnCoordinator | None" = None,
        on_unhealthy: Callable[[str], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._identity = identity
        self._track_sid = track_sid
        self._vad_stream = vad.stream()
        self._stt_stream = stt.stream()
        # Phase 3: None unless semantic turn detection is ALSO enabled and
        # its detector constructed successfully (see
        # PocAgentSession._maybe_start_vad_stt_pipeline) - every call site
        # below is a no-op when this is None, so Phase 2 behavior is
        # completely unaffected when Phase 3 is off (the default).
        self._candidate_turn = candidate_turn
        # Phase 4 (Step 11): called at most as a courtesy signal when the
        # VAD or STT stream dies unexpectedly - PocAgentSession supplies
        # its _fallback_to_browser_control here (a no-op unless semantic
        # turn CONTROL is genuinely active for this session, see that
        # method). This class still never reaches back into turn state
        # itself; it only ever invokes this one bounded callback.
        self._on_unhealthy = on_unhealthy
        self._speech_started_at: float | None = None
        self._partial_count = 0
        self._closed = False
        self._vad_task: "asyncio.Task[None]" = asyncio.ensure_future(self._consume_vad_events())
        self._stt_task: "asyncio.Task[None]" = asyncio.ensure_future(self._consume_stt_events())

    def push_frame(self, frame: "rtc.AudioFrame") -> None:
        """Feeds one frame to both streams (and the Phase 3 candidate-turn
        audio buffer, if active). Each push is independently guarded - a
        failure pushing to (or an already-dead) VAD stream must never
        prevent the SAME frame from reaching STT or the candidate buffer,
        and vice versa."""
        if self._closed:
            return
        try:
            self._vad_stream.push_frame(frame)
        except Exception:
            logger.exception(
                "student_vad_push_frame_failed session_id=%s track=%s", self._session_id, self._track_sid,
            )
        try:
            self._stt_stream.push_frame(frame)
        except Exception:
            logger.exception(
                "student_stt_push_frame_failed session_id=%s track=%s", self._session_id, self._track_sid,
            )
        if self._candidate_turn is not None:
            try:
                self._candidate_turn.push_audio(frame)
            except Exception:
                logger.exception(
                    "student_turn_detector_push_audio_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )

    async def _consume_vad_events(self) -> None:
        from livekit.agents import vad as agents_vad

        try:
            async for event in self._vad_stream:
                if event.type == agents_vad.VADEventType.START_OF_SPEECH:
                    self._speech_started_at = time.monotonic()
                    logger.info(
                        "student_vad_speech_started session_id=%s identity=%s track=%s",
                        self._session_id, self._identity, self._track_sid,
                    )
                    if self._candidate_turn is not None:
                        self._candidate_turn.on_speech_started()
                elif event.type == agents_vad.VADEventType.END_OF_SPEECH:
                    logger.info(
                        "student_vad_speech_ended session_id=%s identity=%s track=%s "
                        "speech_duration_ms=%.0f silence_duration_ms=%.0f",
                        self._session_id, self._identity, self._track_sid,
                        event.speech_duration * 1000, event.silence_duration * 1000,
                    )
                    # Phase 3 trigger point (Step 6) - see
                    # _CandidateTurnCoordinator's class docstring for why
                    # THIS event (not a second, duplicate silence-timeout)
                    # is the semantic-detector evaluation boundary.
                    if self._candidate_turn is not None:
                        self._candidate_turn.on_speech_ended(
                            speech_duration_s=event.speech_duration, silence_duration_s=event.silence_duration,
                        )
                # INFERENCE_DONE (per-window speech probability) is
                # deliberately never logged - it fires far too often
                # (roughly every capabilities.update_interval, ~32ms) to be
                # a "concise, non-spammy" diagnostic; START_OF_SPEECH/
                # END_OF_SPEECH alone already satisfy Step 6's
                # speech_started/speech_ended requirement.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "student_vad_consume_failed session_id=%s identity=%s track=%s",
                self._session_id, self._identity, self._track_sid,
            )
            if self._on_unhealthy is not None:
                self._on_unhealthy("vad_consume_failed")

    async def _consume_stt_events(self) -> None:
        from livekit.agents import stt as agents_stt

        try:
            async for event in self._stt_stream:
                if event.type == agents_stt.SpeechEventType.END_OF_SPEECH:
                    # Phase 7 (diagnostic only): checked BEFORE the
                    # `alternatives`-empty guard below, since this event
                    # (derived by the plugin from Deepgram's own
                    # speech_final - a SEPARATE, differently-tuned
                    # endpointing signal from Silero's VAD) never carries
                    # any alternatives and would otherwise be silently
                    # skipped by that guard. The Deepgram plugin already
                    # emits this but worker.py has never consumed it
                    # before now. Logged ONLY for future correlation/
                    # analysis - deliberately does NOT submit, shorten any
                    # timer, or otherwise influence any decision (see the
                    # Phase 7 design doc's explicit "log only, not sole
                    # authority, not even an accelerator yet" decision).
                    pending_kind = pending_turn_id = pending_elapsed_ms = None
                    if self._candidate_turn is not None:
                        pending_kind, pending_turn_id, pending_elapsed_ms = (
                            self._candidate_turn.diagnostic_pending_resolution_state()
                        )
                    logger.info(
                        "student_stt_native_end_of_speech session_id=%s identity=%s track=%s "
                        "semantic_turn_id=%s pending_resolution_kind=%s "
                        "pending_resolution_elapsed_ms=%s",
                        self._session_id, self._identity, self._track_sid,
                        pending_turn_id or "-", pending_kind or "-",
                        f"{pending_elapsed_ms:.0f}" if pending_elapsed_ms is not None else "-",
                    )
                    continue
                if not event.alternatives:
                    continue
                text = event.alternatives[0].text
                elapsed_ms = (
                    (time.monotonic() - self._speech_started_at) * 1000
                    if self._speech_started_at is not None else None
                )
                if event.type == agents_stt.SpeechEventType.INTERIM_TRANSCRIPT:
                    self._partial_count += 1
                    logger.info(
                        "student_stt_partial session_id=%s identity=%s track=%s "
                        "partial_count=%d elapsed_since_speech_started_ms=%s text=%r",
                        self._session_id, self._identity, self._track_sid, self._partial_count,
                        f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-", text,
                    )
                elif event.type == agents_stt.SpeechEventType.FINAL_TRANSCRIPT:
                    # Phase 7 (diagnostic only - see the Phase 7 design doc's
                    # "STT final diagnostics" note): these fields are already
                    # computed by the plugin (SpeechData.start_time/end_time
                    # from Deepgram's own per-word timestamps, request_id/
                    # confidence from the same response) but were previously
                    # discarded before ever reaching a log line. Logged here
                    # PURELY for future observability/analysis - never read
                    # by on_final_transcript, never used for ordering or
                    # ownership decisions (that remains explicitly out of
                    # scope for this phase).
                    alt = event.alternatives[0]
                    logger.info(
                        "student_stt_final session_id=%s identity=%s track=%s "
                        "partial_count=%d elapsed_since_speech_started_ms=%s text=%r "
                        "start_time=%s end_time=%s confidence=%s request_id=%s",
                        self._session_id, self._identity, self._track_sid, self._partial_count,
                        f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-", text,
                        f"{alt.start_time:.3f}", f"{alt.end_time:.3f}", f"{alt.confidence:.3f}",
                        event.request_id or "-",
                    )
                    # Phase 3: hands this final segment to the EXPERIMENTAL
                    # candidate-turn transcript accumulator (see
                    # _CandidateTurnCoordinator.on_final_transcript) - this
                    # is the ONLY place Phase 3's transcript accumulation
                    # happens; the reset below is Phase 2's OWN, separate,
                    # per-utterance bookkeeping and is unchanged.
                    if self._candidate_turn is not None:
                        self._candidate_turn.on_final_transcript(text)
                    # Reset for the NEXT utterance - Phase 2 deliberately
                    # treats every final transcript as its own segment (no
                    # merging across silence gaps at THIS layer - Phase 3's
                    # _CandidateTurnCoordinator does its OWN, separate
                    # merging for semantic evaluation only).
                    self._partial_count = 0
                    self._speech_started_at = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "student_stt_consume_failed session_id=%s identity=%s track=%s",
                self._session_id, self._identity, self._track_sid,
            )
            if self._on_unhealthy is not None:
                self._on_unhealthy("stt_consume_failed")

    async def aclose(self) -> None:
        """Stops both consumer tasks and closes both streams. Best-effort at
        every step - one failed close must not skip the others - so a
        Deepgram-side error on teardown can never leave the VAD stream (or
        vice versa) leaking past the ingest task's own lifetime."""
        self._closed = True
        for task in (self._vad_task, self._stt_task):
            if not task.done():
                task.cancel()
        for task in (self._vad_task, self._stt_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "student_vad_stt_consumer_task_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
        try:
            await self._vad_stream.aclose()
        except Exception:
            logger.exception(
                "student_vad_stream_close_failed session_id=%s track=%s", self._session_id, self._track_sid,
            )
        try:
            await self._stt_stream.aclose()
        except Exception:
            logger.exception(
                "student_stt_stream_close_failed session_id=%s track=%s", self._session_id, self._track_sid,
            )
        if self._candidate_turn is not None:
            try:
                await self._candidate_turn.aclose()
            except Exception:
                logger.exception(
                    "student_turn_detector_coordinator_close_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )


class _CandidateTurnCoordinator:
    """Phase 3 (EXPERIMENTAL, OBSERVATIONAL ONLY): layers semantic turn
    detection on top of Phase 2's VAD/STT events. Owns a BOUNDED, in-memory-
    only candidate-turn audio buffer and an EXPERIMENTAL accumulated
    candidate transcript - NEITHER is ever the real transcript, never
    written to DB/session history, and an END decision here NEVER calls
    PocAgentSession._handle_student_turn/_run_turn or publishes student_text
    - this class holds no reference to PocAgentSession or any of that state
    at all, so there is no path by which it could.

    Trigger (Step 6): Silero VAD's own END_OF_SPEECH already provides a
    well-tuned speech/silence boundary (min_silence_duration=0.55s by
    default - see Phase 2's _StudentVadSttPipeline). Reusing it here avoids
    re-implementing a second, redundant silence-timeout inside the detector
    - Smart Turn's OWN reference implementation (pipecat's BaseSmartTurn)
    has its own stop_secs silence-timeout/audio-accumulation logic, but this
    class deliberately does NOT use it (see turn_detector.py's module
    docstring for why SmartTurnDetector is stateless per-call instead).

    STT/VAD race (Step 9): Deepgram's FINAL_TRANSCRIPT for the segment that
    just ended is NOT guaranteed to have arrived by the time VAD's
    END_OF_SPEECH fires - they are independent endpointing pipelines. On
    END_OF_SPEECH, _evaluate_boundary waits up to _STT_FINAL_GRACE_SECONDS
    for the NEXT final transcript (an asyncio.Event, raced against a short
    timeout) before evaluating anyway - a bounded grace window to collect
    the matching STT result, NOT a general conversational delay: a final
    that arrives promptly short-circuits the wait immediately.

    Phase 4 (Step 4/7): `on_end`, when supplied (only when
    PocAgentSession._semantic_control_active is True for this session - see
    _maybe_start_turn_detector), is awaited (via a fire-and-forget
    asyncio.ensure_future, not awaited inline - see _evaluate_boundary) with
    a STABLE per-candidate-turn id and the accumulated transcript once a
    boundary evaluates to END with a non-empty transcript. This is the ONLY
    way an END decision can ever reach PocAgentSession/patient generation -
    when `on_end` is None (Phase 3 default / control off), this class's
    behavior is byte-for-byte the observational-only Phase 3 behavior.

    Phase 5A (Step 4/5/7): `is_patient_speaking`/`on_barge_in`, when both
    supplied (only when PocAgentSession._semantic_barge_in_active is True -
    see _maybe_start_turn_detector), let a SEPARATE, smaller classification
    (turn_detector.classify_barge_in) run on transcript heard WHILE the
    patient is speaking - a different question from Smart Turn's own
    HOLD/END ("has the student's utterance finished") entirely. See
    on_final_transcript/on_speech_ended/_promote_barge_in_to_candidate_turn
    for the state machine (deliberately NOT a bigger one - a buffer plus a
    single classify() call). When either callback is None (the Phase 4
    default), student speech during patient audio is still observed (VAD/
    STT never pause) but never classified or acted on - byte-for-byte
    Phase 4 behavior.

    Phase 6 (Step 3/4/6): `on_hold`, when supplied (only when
    PocAgentSession._backchannel_enabled is True), is called (sync - it
    only ever kicks off a background task on the SESSION side; this class
    never awaits or owns any backchannel state itself) on a HOLD decision
    whose candidate transcript clears a small content bar (Step 4) -
    PATIENT_BACKCHANNEL is a PocAgentSession-only concept; this coordinator
    never tracks backchannel scheduling/playback state. `on_student_resumed`
    fires unconditionally at the top of on_speech_started - the earliest
    possible "the student is talking again" signal, exactly what a
    scheduled-or-playing backchannel needs to yield immediately (Step 6/7).
    `is_backchannel_echo`, when supplied, is checked at the very TOP of
    on_final_transcript (Step 13) - before even the Phase 5A barge-in
    branch, since a backchannel is never "patient speaking" in the
    _speaking_client_turn_id sense and could arrive regardless of any other
    state. All three are None together whenever backchanneling is off -
    zero behavior change from Phase 5B.

    Phase 7 (EXPERIMENTAL semantic resolution timers): `resolution_timers_enabled`
    (only True when PocAgentSession.settings.semantic_resolution_timers_active
    is True - see _maybe_start_turn_detector) gates a SINGLE pending-resolution
    slot (`_pending_kind`/`_pending_turn_id`/`_pending_task` - never two
    independent timer states that could coexist) armed on EVERY HOLD or END
    decision once semantic control is active: HOLD arms a bounded recovery
    deadline (fixes "Smart Turn HOLDs a genuinely complete question and the
    student is then left with silence forever" - there was previously no
    recovery from a false HOLD at all); END arms a short universal grace
    instead of resetting/submitting immediately (fixes "a brief natural
    pause splits one utterance into two turns"). Both are cancelled the
    instant on_speech_started fires (same turn continues, nothing submitted)
    and otherwise converge on the SAME shared commit path
    (_commit_pending_resolution) that HOLD/END already used before this
    phase - see _arm_pending_resolution/_cancel_pending_resolution. When
    False (the default), HOLD/END behave byte-for-byte as they did before
    this phase existed. `on_before_commit`, when supplied, is called
    (sync, like on_student_resumed) immediately before a commit proceeds -
    lets PocAgentSession explicitly stop any pending/playing backchannel
    through its EXISTING cancellation mechanism before the real patient
    turn's audio starts publishing, rather than relying only on the
    "new speech cancels it" invariant (which does not hold for a
    HOLD-recovery commit - by definition no new speech occurred).
    """

    def __init__(
        self,
        *,
        session_id: str,
        identity: str,
        track_sid: str,
        detector: SemanticTurnDetector,
        sample_rate: int,
        on_end: Callable[[str, str], Awaitable[None]] | None = None,
        on_unhealthy: Callable[[str], None] | None = None,
        is_patient_speaking: Callable[[], str | None] | None = None,
        on_barge_in: Callable[[str, str, str], Awaitable[None]] | None = None,
        on_hold: Callable[[str, str, "float | None"], None] | None = None,
        on_student_resumed: Callable[[], None] | None = None,
        is_backchannel_echo: Callable[[str], bool] | None = None,
        resolution_timers_enabled: bool = False,
        on_before_commit: Callable[[], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._identity = identity
        self._track_sid = track_sid
        self._detector = detector
        self._sample_rate = sample_rate
        self._max_candidate_samples = int(_MAX_CANDIDATE_AUDIO_SECONDS * sample_rate)
        self._candidate_audio: "collections.deque[np.ndarray]" = collections.deque()
        self._candidate_audio_samples = 0
        self._candidate_segments: list[str] = []
        self._turn_started_at: float | None = None
        # Phase 4 (Step 7): assigned once per candidate turn (on its FIRST
        # speech_started, alongside _turn_started_at - see on_speech_started)
        # and cleared on reset. Stable through HOLD -> continuation -> END,
        # unique within this session (a monotonic per-coordinator sequence -
        # a coordinator is 1:1 with one session's one student audio track),
        # and prefixed distinctly from browser clientTurnId's own formats
        # (a bare UUID, or "turn-<ts>-<rand>") so the two id spaces can never
        # collide (Step 7).
        self._candidate_turn_id: str | None = None
        self._turn_id_seq = itertools.count(1)
        self._pending_final_event = asyncio.Event()
        self._closed = False
        self._eval_task: "asyncio.Task[None] | None" = None
        self._on_end = on_end
        self._on_unhealthy = on_unhealthy
        self._is_patient_speaking = is_patient_speaking
        self._on_barge_in = on_barge_in
        self._on_hold = on_hold
        self._on_student_resumed = on_student_resumed
        self._is_backchannel_echo = is_backchannel_echo
        self._resolution_timers_enabled = resolution_timers_enabled
        self._on_before_commit = on_before_commit
        # Phase 7: anchor timestamp for _HOLD_RECOVERY_SECONDS/
        # _PENDING_END_GRACE_SECONDS deadlines - captured at the START of
        # on_speech_ended (the VAD END_OF_SPEECH boundary), NOT when Smart
        # Turn happens to finish evaluating, so STT-grace-wait/inference
        # latency already spent counts against the budget rather than
        # extending it. Overwritten on every boundary; only the value at
        # the boundary that actually produces a HOLD/END decision matters.
        self._vad_end_at: float | None = None
        # Phase 7: the SINGLE pending-resolution slot (Step: "one semantic
        # candidate -> at most one pending resolution" invariant) - never
        # two independent timer fields that could accidentally both be
        # armed. `_pending_kind` is one of _HOLD_RECOVERY_KIND/
        # _PENDING_END_KIND when armed, None otherwise; the other four
        # fields are only meaningful together with it (all None when
        # `_pending_kind` is None) - see _arm_pending_resolution/
        # _cancel_pending_resolution/_commit_pending_resolution.
        self._pending_kind: str | None = None
        self._pending_turn_id: str | None = None
        self._pending_task: "asyncio.Task[None] | None" = None
        self._pending_armed_at: float | None = None
        self._pending_probability: float | None = None
        # Phase 5A: text accumulated WHILE the patient is speaking, re-
        # classified as a whole on every new final (Step 9) - kept entirely
        # separate from _candidate_segments until/unless promoted (Step 7.9)
        # so an acknowledgement can never contaminate the next real student
        # turn (Step 8).
        self._barge_in_buffer: list[str] = []
        # Phase 4 (Step 11): consecutive (never reset by a successful HOLD/
        # END - only by aclose) detector-evaluation failures - see
        # _evaluate_boundary's except block and
        # _SEMANTIC_ERROR_FALLBACK_THRESHOLD.
        self._consecutive_errors = 0
        # Step 13: lightweight in-memory-only counters, logged as one
        # summary line on aclose() - not a monitoring platform, just enough
        # to judge the POC.
        self._metric_boundaries = 0
        self._metric_hold = 0
        self._metric_end = 0
        self._metric_errors = 0
        self._metric_latency_sum_ms = 0.0
        self._metric_latency_count = 0

    def push_audio(self, frame: "rtc.AudioFrame") -> None:
        """Appends one frame's samples to the bounded candidate-turn audio
        buffer. Frames arrive already at self._sample_rate (see
        _ingest_student_audio's AudioStream construction) - no resampling
        performed here. Trims from the OLDEST end once over the cap, mirroring
        SmartTurnDetector's own "keep the most recent window" truncation."""
        if self._closed:
            return
        import numpy as np

        samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        self._candidate_audio.append(samples)
        self._candidate_audio_samples += len(samples)
        while self._candidate_audio_samples > self._max_candidate_samples and len(self._candidate_audio) > 1:
            dropped = self._candidate_audio.popleft()
            self._candidate_audio_samples -= len(dropped)

    def on_speech_started(self) -> None:
        """Only records the FIRST speech_started of a candidate turn - a
        HOLD-continued turn's later speech segments must not reset
        total_turn_duration_ms back to zero.

        Phase 5A: while the patient is genuinely speaking AND this speech
        hasn't already been promoted to a real barge-in candidate turn,
        deliberately does NOT start a normal candidate turn yet - the
        transcript is still being classified (see on_final_transcript). A
        normal turn only begins here once patient speech ends naturally,
        or immediately via _promote_barge_in_to_candidate_turn on
        TRUE_BARGE_IN.

        Phase 6 (Step 6/7): fires on_student_resumed FIRST (once past the
        closed-check, matching every other method's own discipline here) -
        the earliest possible "the student is talking again" signal,
        regardless of every other branch below. A harmless no-op when there
        is no pending/playing backchannel to cancel (PocAgentSession checks
        that itself); this class has no idea whether one exists.

        Phase 7: cancels any armed pending-resolution timer (HOLD recovery
        or END grace) FIRST, before on_student_resumed and before any
        candidate-state mutation below - the earliest possible point, so a
        HOLD_RECOVERY/PENDING_END deadline can never fire once genuinely
        new speech has started. A harmless no-op when nothing is armed."""
        if self._closed:
            return
        self._cancel_pending_resolution(reason="student_resumed")
        if self._on_student_resumed is not None:
            self._on_student_resumed()
        if (
            self._is_patient_speaking is not None
            and self._on_barge_in is not None
            and self._is_patient_speaking() is not None
            and self._candidate_turn_id is None
        ):
            return
        if self._turn_started_at is None:
            self._turn_started_at = time.monotonic()
            self._candidate_turn_id = f"semantic-{self._session_id}-{next(self._turn_id_seq)}"

    def on_final_transcript(self, text: str) -> None:
        """Appends ONE STT final segment to the EXPERIMENTAL candidate
        transcript (never the real transcript - see class docstring) and
        signals any evaluation currently waiting in its STT-final grace
        window (Step 9).

        Phase 5A: when the patient is genuinely speaking and this segment
        hasn't already been promoted to a real candidate turn, routes
        through the barge-in buffer/classifier INSTEAD of the normal
        accumulation below - see _handle_final_transcript_during_patient_speech.
        The `self._candidate_turn_id is None` guard is what makes this
        re-entrant-safe: once TRUE_BARGE_IN promotes a turn, every
        SUBSEQUENT final (even while _speaking_client_turn_id is still
        technically non-zero for a few more ms, before cancellation
        actually lands) falls through to normal accumulation below, not a
        second classification pass (Step 10: duplicate barge-in signal).

        Phase 6 (Step 13): is_backchannel_echo, when supplied, is checked
        HERE FIRST - before even the Phase 5A barge-in branch below - since
        a backchannel echo could arrive regardless of _speaking_client_turn_id
        (a backchannel never sets it, unlike a real patient response) and
        must never be treated as either barge-in content OR normal
        candidate-turn text. A match is discarded outright, never buffered
        anywhere."""
        if self._closed:
            return
        text = text.strip()
        if not text:
            self._pending_final_event.set()
            return

        if self._is_backchannel_echo is not None and self._is_backchannel_echo(text):
            logger.info(
                "patient_backchannel_echo_discarded session_id=%s identity=%s track=%s transcript=%r",
                self._session_id, self._identity, self._track_sid, text,
            )
            self._pending_final_event.set()
            return

        if (
            self._is_patient_speaking is not None
            and self._on_barge_in is not None
            and self._candidate_turn_id is None
        ):
            speaking_client_turn_id = self._is_patient_speaking()
            if speaking_client_turn_id is not None:
                self._handle_final_transcript_during_patient_speech(text, speaking_client_turn_id)
                self._pending_final_event.set()
                return

        if self._barge_in_buffer:
            # Patient stopped speaking between finals before the buffer's
            # ack/undecided content ever resolved via on_speech_ended (rare
            # timing edge) - discard rather than silently merging
            # unclassified during-speech chatter into a real turn.
            logger.info(
                "semantic_barge_in_acknowledgement session_id=%s identity=%s track=%s "
                "reason=patient_stopped_speaking_mid_buffer transcript=%r",
                self._session_id, self._identity, self._track_sid, " ".join(self._barge_in_buffer),
            )
            self._barge_in_buffer = []

        self._candidate_segments.append(text)
        self._pending_final_event.set()

    def _handle_final_transcript_during_patient_speech(
        self, text: str, speaking_client_turn_id: str
    ) -> None:
        """Phase 5A (Step 5/9): re-classifies the FULL accumulated
        during-patient-speech buffer (not just this one segment) on every
        call - this is what lets a provisional "yeah" correctly upgrade to
        TRUE_BARGE_IN once real continuation arrives in a LATER final for
        the SAME speech segment, without this method needing to track
        history beyond the buffer itself."""
        self._barge_in_buffer.append(text)
        joined = " ".join(self._barge_in_buffer).strip()
        decision = classify_barge_in(joined)
        logger.info(
            "semantic_barge_in_candidate session_id=%s identity=%s track=%s "
            "patient_client_turn_id=%s classification=%s transcript=%r",
            self._session_id, self._identity, self._track_sid, speaking_client_turn_id,
            decision.value, joined,
        )
        if decision != BargeInDecision.TRUE_BARGE_IN:
            # ACKNOWLEDGEMENT or UNDECIDED - keep buffering, no action yet
            # (Step 8/9). Resolved (acted on or discarded) once patient
            # speech for this segment ends - see on_speech_ended.
            return

        new_candidate_turn_id = self._promote_barge_in_to_candidate_turn(joined)
        logger.info(
            "semantic_barge_in_confirmed session_id=%s identity=%s track=%s "
            "patient_client_turn_id=%s student_candidate_turn_id=%s transcript=%r",
            self._session_id, self._identity, self._track_sid, speaking_client_turn_id,
            new_candidate_turn_id, joined,
        )
        assert self._on_barge_in is not None  # guarded by the caller
        asyncio.ensure_future(self._on_barge_in(speaking_client_turn_id, joined, new_candidate_turn_id))

    def _promote_barge_in_to_candidate_turn(self, text: str) -> str:
        """Phase 5A (Step 7.9): the barge-in transcript-so-far becomes the
        START of a brand-new candidate turn - a FRESH id (never reusing one
        from a previous turn), seeded segments, fresh turn-start time.
        Everything downstream (further finals, the next VAD boundary, Smart
        Turn HOLD/END) proceeds through the EXACT SAME normal machinery
        used when the patient isn't speaking at all - no second pipeline."""
        self._turn_started_at = time.monotonic()
        self._candidate_turn_id = f"semantic-{self._session_id}-{next(self._turn_id_seq)}"
        self._candidate_segments = [text]
        self._barge_in_buffer = []
        return self._candidate_turn_id

    def on_speech_ended(self, *, speech_duration_s: float, silence_duration_s: float) -> None:
        """Schedules ONE boundary evaluation. If a PREVIOUS boundary's
        evaluation is still in flight, this one is skipped rather than
        overlapping two concurrent inferences - the candidate buffer/
        transcript keep accumulating regardless, so the NEXT end-of-speech
        (once the in-flight one resolves) evaluates against the by-then-
        larger context anyway; nothing is lost, only deferred.

        Phase 5A (Step 8): if this speech segment happened entirely during
        patient speech and was NEVER promoted to a real barge-in (i.e. it
        stayed ACKNOWLEDGEMENT/UNDECIDED the whole time), discard the
        buffer here and skip Smart Turn evaluation for this boundary
        entirely - never spend a model inference on a "mm-hmm", and never
        let it become a standalone student turn (Step 8)."""
        if self._closed:
            return
        if self._barge_in_buffer and self._candidate_turn_id is None:
            logger.info(
                "semantic_barge_in_acknowledgement session_id=%s identity=%s track=%s "
                "reason=speech_ended_unresolved transcript=%r",
                self._session_id, self._identity, self._track_sid, " ".join(self._barge_in_buffer),
            )
            self._barge_in_buffer = []
            self._pending_final_event.clear()
            return
        # Phase 7: anchor for HOLD_RECOVERY/PENDING_END deadlines - see
        # __init__'s _vad_end_at docstring note. Captured unconditionally
        # (even if the in-flight guard below ends up skipping evaluation
        # for THIS boundary) since only the value captured at whichever
        # boundary actually produces a decision matters.
        self._vad_end_at = time.monotonic()
        self._metric_boundaries += 1
        self._pending_final_event.clear()
        if self._eval_task is not None and not self._eval_task.done():
            logger.info(
                "student_turn_detector_boundary_skipped session_id=%s identity=%s track=%s "
                "reason=evaluation_in_progress",
                self._session_id, self._identity, self._track_sid,
            )
            return
        self._eval_task = asyncio.ensure_future(
            self._evaluate_boundary(speech_duration_s=speech_duration_s, silence_duration_s=silence_duration_s)
        )

    async def _evaluate_boundary(self, *, speech_duration_s: float, silence_duration_s: float) -> None:
        """Step 10 safety fallback: EVERY failure mode here (timeout,
        malformed buffer, detector exception) is caught and logged; the
        candidate state is left exactly as-is (never cleared, never
        corrupted) so the next boundary can still evaluate normally. Never
        raises into _StudentVadSttPipeline, let alone the conversation
        path - this coroutine has no reference to reach it even if it
        wanted to."""
        import numpy as np

        try:
            grace_start = time.monotonic()
            stt_final_pending = True
            try:
                await asyncio.wait_for(self._pending_final_event.wait(), timeout=_STT_FINAL_GRACE_SECONDS)
                stt_final_pending = False
            except asyncio.TimeoutError:
                # Evaluate with whatever transcript is currently available -
                # see class docstring's Step 9 note. Not an error.
                pass

            if self._closed:
                return

            audio = (
                np.concatenate(list(self._candidate_audio))
                if self._candidate_audio else np.zeros(0, dtype=np.float32)
            )
            transcript = " ".join(self._candidate_segments).strip()
            total_turn_ms = (
                (time.monotonic() - self._turn_started_at) * 1000
                if self._turn_started_at is not None else None
            )

            context = TurnContext(
                audio=audio,
                audio_sample_rate=self._sample_rate,
                transcript=transcript,
                segment_count=len(self._candidate_segments),
                pause_ms=silence_duration_s * 1000,
                speech_duration_ms=speech_duration_s * 1000,
                total_turn_duration_ms=total_turn_ms,
            )
            result = await self._detector.evaluate(context)
            self._metric_latency_sum_ms += result.inference_ms
            self._metric_latency_count += 1
            probability_str = f"{result.probability:.4f}" if result.probability is not None else "-"
            total_turn_str = f"{total_turn_ms:.0f}" if total_turn_ms is not None else "-"

            if result.decision == TurnDecision.END:
                self._metric_end += 1
                self._consecutive_errors = 0
                candidate_turn_id = self._candidate_turn_id
                logger.info(
                    "student_turn_detector_decision session_id=%s identity=%s track=%s decision=END "
                    "detector=%s probability=%s inference_ms=%.1f candidate_segment_count=%d "
                    "total_turn_duration_ms=%s stt_final_pending=%s candidate_text=%r "
                    "semantic_turn_id=%s",
                    self._session_id, self._identity, self._track_sid, result.detector, probability_str,
                    result.inference_ms, len(self._candidate_segments), total_turn_str,
                    stt_final_pending, transcript, candidate_turn_id or "-",
                )
                if self._on_end is None:
                    # Phase 3 default (control off/not-yet-active) -
                    # byte-for-byte the original observational-only
                    # behavior: reset, nothing else.
                    self._reset_candidate_turn()
                elif not transcript or candidate_turn_id is None:
                    # Step 10: END with an empty/unusable candidate
                    # transcript - never submit an empty turn to OpenAI.
                    logger.info(
                        "semantic_turn_end_empty_transcript_skipped session_id=%s identity=%s track=%s "
                        "semantic_turn_id=%s",
                        self._session_id, self._identity, self._track_sid, candidate_turn_id or "-",
                    )
                    self._reset_candidate_turn()
                elif not self._resolution_timers_enabled:
                    # Phase 7 flag OFF (default) - byte-for-byte the
                    # pre-Phase-7 behavior: reset BEFORE firing submission
                    # (Step 4/10 - the NEXT candidate turn, if speech starts
                    # again while this one's patient response is still
                    # generating, gets a fresh id, and a stray
                    # re-evaluation of THIS boundary can never resubmit the
                    # same candidate_turn_id), then fire-and-forget on_end.
                    self._reset_candidate_turn()
                    # Fire-and-forget (Step 4): the actual patient-generation
                    # pipeline (turn lock, OpenAI, TTS, audio publish) can
                    # take many seconds - awaiting it here would block this
                    # coordinator's OWN boundary-evaluation serialization
                    # (see on_speech_ended's in-flight guard) for no reason;
                    # PocAgentSession's own _turn_lock is what actually
                    # serializes patient generation.
                    asyncio.ensure_future(self._on_end(candidate_turn_id, transcript))
                else:
                    # Phase 7 flag ON: universal END grace - EVERY END gets
                    # this, deliberately no lexical/grammar heuristic (see
                    # the Phase 7 design doc for why a text classifier was
                    # rejected as unreliable). Do NOT reset yet - preserve
                    # candidate audio/transcript/turn id exactly like a
                    # HOLD would, so a brief natural pause can still rejoin
                    # this SAME turn if the student resumes before the
                    # deadline (see on_speech_started's
                    # _cancel_pending_resolution call).
                    self._arm_pending_resolution(kind=_PENDING_END_KIND, probability=result.probability)
            else:
                self._metric_hold += 1
                self._consecutive_errors = 0
                logger.info(
                    "student_turn_detector_decision session_id=%s identity=%s track=%s decision=HOLD "
                    "detector=%s probability=%s inference_ms=%.1f candidate_segment_count=%d "
                    "pause_ms=%.0f stt_final_pending=%s candidate_text=%r",
                    self._session_id, self._identity, self._track_sid, result.detector, probability_str,
                    result.inference_ms, len(self._candidate_segments), silence_duration_s * 1000,
                    stt_final_pending, transcript,
                )
                if self._on_end is not None:
                    # Step 12: a turn-CONTROL-specific event (distinct from
                    # the always-on Phase 3 line above) so an operator can
                    # filter to only the runs where semantic control is
                    # genuinely driving the conversation.
                    logger.info(
                        "semantic_turn_hold session_id=%s semantic_turn_id=%s candidate_segment_count=%d "
                        "candidate_text=%r probability=%s turn_source=%s",
                        self._session_id, self._candidate_turn_id or "-", len(self._candidate_segments),
                        transcript, probability_str, TurnSource.SERVER_SEMANTIC.value,
                    )
                # Phase 6 (Step 4): content pre-filter here (this class owns
                # the transcript) - PocAgentSession does its OWN, separate
                # session-state eligibility check (already played this turn,
                # patient not thinking/speaking, etc.) before actually
                # scheduling anything. Never fires for a bare filler
                # fragment ("So...", "Um...") - Step 4/13's own requirement.
                if (
                    self._on_hold is not None
                    and self._candidate_turn_id is not None
                    and len(transcript.split()) >= _MIN_BACKCHANNEL_WORDS
                ):
                    self._on_hold(self._candidate_turn_id, transcript, result.probability)
                # HOLD: deliberately preserve candidate_audio/candidate_segments/
                # turn_started_at - see class docstring.
                # Phase 7: arm the false-HOLD recovery deadline whenever
                # resolution timers are enabled AND semantic control is
                # genuinely active (on_end is the same "is this authoritative"
                # signal every other Phase-4+ feature here already gates on) -
                # fixes "Smart Turn HOLDs a genuinely complete question and
                # the student is left with silence forever" (there was
                # previously NO recovery from a false HOLD at all). A no-op
                # when the flag is off - byte-for-byte pre-Phase-7 behavior.
                if (
                    self._resolution_timers_enabled
                    and self._on_end is not None
                    and self._candidate_turn_id is not None
                ):
                    self._arm_pending_resolution(kind=_HOLD_RECOVERY_KIND, probability=result.probability)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._metric_errors += 1
            self._consecutive_errors += 1
            logger.exception(
                "student_turn_detector_error session_id=%s identity=%s track=%s consecutive_errors=%d",
                self._session_id, self._identity, self._track_sid, self._consecutive_errors,
            )
            if (
                self._on_unhealthy is not None
                and self._consecutive_errors >= _SEMANTIC_ERROR_FALLBACK_THRESHOLD
            ):
                self._on_unhealthy("detector_repeated_errors")

    def _arm_pending_resolution(self, *, kind: str, probability: "float | None") -> None:
        """Phase 7: arms the SINGLE pending-resolution slot for the CURRENT
        candidate turn. Defensively cancels/clears any existing one first
        (Step: "entering a new resolution must defensively cancel/clear an
        existing one") - should be structurally unreachable given both
        callers' own gating (a candidate turn only ever produces ONE
        HOLD/END decision per boundary), but cheap to guarantee regardless.
        No-op if there is no active candidate turn to arm against
        (defensive - both callers already check this).

        Deadline is anchored to self._vad_end_at (the VAD END_OF_SPEECH
        timestamp for the boundary that produced this decision), NOT to
        `time.monotonic()` right now - see __init__'s _vad_end_at note for
        why this matters (STT-grace-wait/inference latency already spent
        must count AGAINST the budget, never stack on top of it)."""
        turn_id = self._candidate_turn_id
        if turn_id is None:
            return
        self._cancel_pending_resolution(reason="superseded")
        configured_seconds = (
            _HOLD_RECOVERY_SECONDS if kind == _HOLD_RECOVERY_KIND else _PENDING_END_GRACE_SECONDS
        )
        anchor = self._vad_end_at if self._vad_end_at is not None else time.monotonic()
        deadline = anchor + configured_seconds
        now = time.monotonic()
        delay = max(0.0, deadline - now)
        self._pending_kind = kind
        self._pending_turn_id = turn_id
        self._pending_armed_at = now
        self._pending_probability = probability
        self._pending_task = asyncio.ensure_future(
            self._resolve_pending_resolution(kind=kind, turn_id=turn_id, delay=delay)
        )
        started_event = (
            "semantic_hold_recovery_started" if kind == _HOLD_RECOVERY_KIND
            else "semantic_pending_end_started"
        )
        probability_str = f"{probability:.4f}" if probability is not None else "-"
        audio_duration_ms = (
            (self._candidate_audio_samples / self._sample_rate) * 1000 if self._sample_rate else 0.0
        )
        transcript = " ".join(self._candidate_segments).strip()
        logger.info(
            "%s session_id=%s identity=%s track=%s semantic_turn_id=%s probability=%s "
            "candidate_text=%r candidate_audio_duration_ms=%.0f configured_seconds=%.3f "
            "remaining_seconds=%.3f",
            started_event, self._session_id, self._identity, self._track_sid, turn_id,
            probability_str, transcript, audio_duration_ms, configured_seconds, delay,
        )

    def _cancel_pending_resolution(self, *, reason: str) -> None:
        """Phase 7: idempotent - a harmless no-op if nothing is armed.
        Cancels the task best-effort (fire-and-forget, matching
        _cancel_pending_backchannel's own discipline elsewhere in this
        file - not awaited here) and clears all pending-resolution fields
        together, so the single-slot invariant can never observe a
        partially-cleared state."""
        if self._pending_kind is None:
            return
        task = self._pending_task
        kind = self._pending_kind
        turn_id = self._pending_turn_id
        armed_at = self._pending_armed_at
        if task is not None and not task.done():
            task.cancel()
        elapsed_ms = (time.monotonic() - armed_at) * 1000 if armed_at is not None else None
        cancelled_event = (
            "semantic_hold_recovery_cancelled" if kind == _HOLD_RECOVERY_KIND
            else "semantic_pending_end_cancelled"
        )
        logger.info(
            "%s session_id=%s identity=%s track=%s semantic_turn_id=%s reason=%s elapsed_ms=%s",
            cancelled_event, self._session_id, self._identity, self._track_sid, turn_id or "-",
            reason, f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-",
        )
        self._pending_kind = None
        self._pending_turn_id = None
        self._pending_task = None
        self._pending_armed_at = None
        self._pending_probability = None

    async def _resolve_pending_resolution(self, *, kind: str, turn_id: str, delay: float) -> None:
        """Phase 7: the deadline-wait half of the pending-resolution
        lifecycle - sleeps, then hands off to _commit_pending_resolution.
        Defensive re-verification after the sleep (same-event-loop-tick
        race safety: "whichever callback executes first establishes the
        state transition, but both possible orderings must be internally
        consistent, exactly-once, no double submission") - if this task has
        been superseded/cancelled by the time the sleep resolves,
        self._pending_task no longer points at US (asyncio.current_task()),
        so we do nothing rather than trust stale closure state."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closed:
                return
            if self._pending_task is not asyncio.current_task():
                # Superseded/cancelled between the sleep resolving and this
                # check - a safe no-op, not an error.
                return
            if self._candidate_turn_id != turn_id:
                # Should be unreachable given _cancel_pending_resolution's
                # own discipline (on_speech_started always cancels BEFORE
                # any candidate-state mutation) - defensive backstop only.
                return
            await self._commit_pending_resolution(kind=kind)
        except asyncio.CancelledError:
            raise

    async def _commit_pending_resolution(self, *, kind: str) -> None:
        """Phase 7: the ONE shared commit path for both HOLD_RECOVERY and
        PENDING_END outcomes - do not create a second submission
        architecture (Step: "Do not create a second submission
        architecture"). Guarantees exactly-once commit: every
        pending-resolution field is cleared BEFORE any further await, so a
        concurrent cancel racing this exact moment finds nothing left to
        cancel. Reuses the EXACT SAME reset-before-submit ordering
        invariant the immediate-END path already used before this phase."""
        turn_id = self._candidate_turn_id
        transcript = " ".join(self._candidate_segments).strip()
        audio_duration_ms = (
            (self._candidate_audio_samples / self._sample_rate) * 1000 if self._sample_rate else 0.0
        )
        armed_at = self._pending_armed_at
        elapsed_ms = (time.monotonic() - armed_at) * 1000 if armed_at is not None else None
        probability = self._pending_probability
        probability_str = f"{probability:.4f}" if probability is not None else "-"

        # Clear pending-resolution bookkeeping BEFORE any further await -
        # exactly-once commit.
        self._pending_kind = None
        self._pending_turn_id = None
        self._pending_task = None
        self._pending_armed_at = None
        self._pending_probability = None

        committed_event = (
            "semantic_hold_recovery_committed" if kind == _HOLD_RECOVERY_KIND
            else "semantic_pending_end_committed"
        )
        logger.info(
            "%s session_id=%s identity=%s track=%s semantic_turn_id=%s elapsed_ms=%s "
            "probability=%s candidate_text=%r candidate_audio_duration_ms=%.0f",
            committed_event, self._session_id, self._identity, self._track_sid, turn_id or "-",
            f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-", probability_str, transcript,
            audio_duration_ms,
        )

        # Explicit cancellation on commit, not just the historical "new
        # speech cancels it" invariant (which does not hold here - a
        # HOLD_RECOVERY commit happens PRECISELY when no new speech
        # occurred): stop any pending/playing backchannel through
        # PocAgentSession's EXISTING mechanism before the real patient
        # turn's audio starts publishing.
        if self._on_before_commit is not None:
            self._on_before_commit()

        # Reset BEFORE firing submission - same invariant the immediate-END
        # path always used (Step 4/10).
        self._reset_candidate_turn()

        if self._on_end is None:
            return
        if not transcript or turn_id is None:
            logger.info(
                "semantic_turn_end_empty_transcript_skipped session_id=%s identity=%s track=%s "
                "semantic_turn_id=%s",
                self._session_id, self._identity, self._track_sid, turn_id or "-",
            )
            return
        # Fire-and-forget - see the immediate-END path's own note on why
        # this is never awaited inline.
        asyncio.ensure_future(self._on_end(turn_id, transcript))

    def diagnostic_pending_resolution_state(self) -> "tuple[str | None, str | None, float | None]":
        """Phase 7: read-only snapshot (kind, turn_id, elapsed_ms) for
        DIAGNOSTIC correlation only (see _StudentVadSttPipeline's
        student_stt_native_end_of_speech log line) - never used for any
        decision, never mutates anything."""
        if self._pending_kind is None or self._pending_armed_at is None:
            return None, None, None
        elapsed_ms = (time.monotonic() - self._pending_armed_at) * 1000
        return self._pending_kind, self._pending_turn_id, elapsed_ms

    def _reset_candidate_turn(self) -> None:
        self._candidate_audio.clear()
        self._candidate_audio_samples = 0
        self._candidate_segments = []
        self._turn_started_at = None
        self._candidate_turn_id = None
        self._barge_in_buffer = []
        self._pending_final_event.clear()
        self._vad_end_at = None
        # Phase 7: defensive symmetry - pending-resolution bookkeeping
        # should already be clear by the time reset runs (both real
        # callers - _commit_pending_resolution and aclose - clear/cancel it
        # themselves first), but never leave stale pending-resolution state
        # pointing at a retired turn id.
        self._pending_kind = None
        self._pending_turn_id = None
        self._pending_task = None
        self._pending_armed_at = None
        self._pending_probability = None

    async def aclose(self) -> None:
        self._closed = True
        if self._eval_task is not None and not self._eval_task.done():
            self._eval_task.cancel()
            try:
                await self._eval_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "student_turn_detector_eval_task_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "student_turn_detector_pending_resolution_task_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
        self._reset_candidate_turn()
        avg_latency = (
            f"{(self._metric_latency_sum_ms / self._metric_latency_count):.1f}"
            if self._metric_latency_count else "-"
        )
        logger.info(
            "student_turn_detector_session_summary session_id=%s identity=%s track=%s "
            "boundaries=%d hold=%d end=%d errors=%d avg_inference_ms=%s",
            self._session_id, self._identity, self._track_sid,
            self._metric_boundaries, self._metric_hold, self._metric_end, self._metric_errors, avg_latency,
        )
        try:
            await self._detector.aclose()
        except Exception:
            logger.exception(
                "student_turn_detector_close_failed session_id=%s track=%s", self._session_id, self._track_sid,
            )


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
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._accepting_audio_producers = True
        self._shutdown_task: "asyncio.Task[None] | None" = None
        self._turn_lock = asyncio.Lock()
        self._session_factory = get_db_factory()
        self._audio_source: "rtc.AudioSource | None" = None
        # Phase D: the AudioSource rate depends on the engine - the legacy
        # ElevenLabs path publishes 16kHz PCM, the Realtime native-voice path
        # publishes 24kHz. Resolved once in start() from settings; the outbound
        # track and every _publish_*_pcm frame must agree with this value.
        self._patient_audio_sample_rate = patient_adapter.LIVEKIT_PCM_SAMPLE_RATE
        # Phase 1 persistent ownership: the worker job owns this provider
        # session. Microphone AudioStreams are replaceable producers only and
        # must never close or clear it.
        self._realtime_session: "RealtimeSession | None" = None
        self._realtime_engine_active = False
        self._realtime_native_agent_active = False
        self._native_agent_runtime: "Any | None" = None
        self._realtime_prompt_agent_active = False
        self._prompt_agent_runtime: "Any | None" = None
        self._realtime_session_started = asyncio.Event()
        self._realtime_configured_ready = False
        self._realtime_ready_task: "asyncio.Task[None] | None" = None
        self._realtime_producer_generation = 0
        self._realtime_producer_track_sid: str | None = None
        self._realtime_producer_attached = False
        self._agent_ready_sent = False
        # P0-3 backend-authoritative generation identity. Advanced only when
        # RealtimeTurnController accepts a deduplicated, non-empty completed
        # transcript; raw speech_started is only a low-latency audio signal.
        # A patient turn receives its generation synchronously at acceptance;
        # any async
        # side effect (persistence, native speech) verifies it still owns the
        # current epoch before proceeding, so a superseded turn can never
        # persist stale disclosure state or speak over the student. See
        # _on_realtime_speech_started / _handle_realtime_turn / _run_realtime_turn.
        self._generation_epoch = 0
        # Shared only by accepted-generation reservation and the final
        # Realtime persistence/disclosure transaction. Never held during model
        # generation, _turn_lock waiting, or audio playback.
        self._generation_authority_lock = threading.Lock()
        # Orthogonal raw-audio candidate state. The Event is set whenever it is
        # safe for Carly to begin speaking and cleared only during an actual
        # speech_started..speech_stopped interval. Teardown always releases it.
        self._realtime_student_speech_active = False
        self._realtime_student_speech_stopped = asyncio.Event()
        self._realtime_student_speech_stopped.set()
        # One cutoff per currently-speaking patient turn, even when duplicate
        # speech_started and accepted-turn confirmation both request it.
        self._realtime_cutoff_turn_id: str | None = None
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
        # Phase 4 (EXPERIMENTAL turn CONTROL - Step 2/11): computed once in
        # start() from settings.semantic_turn_control_active, then a
        # session-scoped ONE-WAY flag - _fallback_to_browser_control can
        # flip it True->False (never back) if the semantic pipeline proves
        # unhealthy mid-session. This is the single value that decides (a)
        # whether browser student_text is ignored as a turn trigger (see
        # _on_data) and (b) what the frontend is told via agent_ready/
        # semantic_fallback (see _send_agent_ready/_fallback_to_browser_control).
        self._semantic_control_active = False
        # Phase 5A (EXPERIMENTAL barge-in - Step 2): computed once in
        # start() alongside _semantic_control_active, from
        # settings.semantic_barge_in_active - meaningless (and forced False)
        # whenever _semantic_control_active is False, and cleared by the
        # SAME one-way _fallback_to_browser_control (barge-in can never
        # outlive the turn-control pipeline it depends on - Step 2/13).
        self._semantic_barge_in_active = False
        # Phase 5B (EXPERIMENTAL spoken-transcript sync - Step 2): computed
        # once in start() from settings.livekit_spoken_transcript_sync_
        # enabled - independent of Phase 4/5A (see config.py's own note).
        # When True, _run_turn publishes patient audio sentence-by-sentence
        # instead of one whole-response blob, so an interruption/mid-
        # response TTS failure can correct the transcript back down to
        # only what genuinely finished playing.
        self._spoken_transcript_sync_active = False
        # Phase 5B (Step 13): bounded dedup set for
        # _finalize_partial_patient_delivery - a defensive backstop, not
        # load-bearing today (see that method's docstring for why
        # finalization is already structurally single-path).
        self._finalized_patient_turn_ids: "OrderedDict[str, None]" = OrderedDict()
        # Phase 6 (EXPERIMENTAL patient backchanneling - Step 2): computed
        # once in start() from settings.patient_backchannel_active -
        # independent of Phase 5A (see config.py's own note), cleared by the
        # SAME one-way _fallback_to_browser_control (backchanneling can
        # never outlive the turn-control pipeline it depends on).
        self._backchannel_enabled = False
        # Phase 7 (EXPERIMENTAL semantic resolution timers - Step 2):
        # computed once in start() from
        # settings.semantic_resolution_timers_active - independent of
        # Phase 5A/6 (see config.py's own note), cleared by the SAME
        # one-way _fallback_to_browser_control (resolution timers can never
        # outlive the turn-control pipeline they depend on).
        self._resolution_timers_enabled = False
        # Phase 6 (Step 3): the ONE task covering BOTH the post-HOLD delay
        # phase and the actual audio-publish phase - cancelling it at
        # EITHER point uniformly satisfies Step 6 (cancel before it starts)
        # and Step 7 (stop it mid-playback) with one mechanism. Deliberately
        # NOT _speaking_client_turn_id/_active_turn_task - a backchannel is
        # never a real patient turn (Step 3's explicit requirement).
        self._backchannel_task: "asyncio.Task[None] | None" = None
        # Phase 6 (Step 8/12): the candidate_turn_id a backchannel has
        # already played for - the max-one-per-semantic-turn cap. A fresh
        # candidate_turn_id (assigned after Smart Turn END resets the
        # coordinator) never matches this, so the NEXT student turn is
        # automatically eligible again - no explicit reset needed anywhere.
        self._backchannel_played_turn_id: str | None = None
        # Phase 6 (Step 7): True only while backchannel PCM is actively
        # being handed to _publish_pcm - lets _cancel_pending_backchannel
        # decide whether a clear_queue() is worth attempting (nothing is
        # queued yet during the pre-playback delay phase).
        self._backchannel_playing = False
        # Phase 6 (Step 13): (normalized_phrase, expiry_monotonic_time) once
        # a backchannel clip's audio genuinely starts publishing - checked
        # by _is_likely_backchannel_echo. None whenever no backchannel has
        # played recently enough to matter.
        self._backchannel_echo_guard: tuple[str, float] | None = None

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
        self._realtime_native_agent_active = startup_settings.realtime_native_agent_active
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
            # Additive field (Phase 4 turn-ID sync fix): distinguishes a
            # deliberate typed Send ("manual_typed") from a browser
            # SpeechRecognition final ("speech_browser" - also the default
            # for any older/legacy frontend build that never sends this
            # field at all, which is the conservative choice: an unknown
            # source is treated as non-authoritative under semantic control,
            # never the other way around).
            source = str(payload.get("source") or "speech_browser")
            is_manual_override = source == "manual_typed"

            self._log_agent_event("livekit_agent_student_packet_received", client_turn_id=client_turn_id)
            logger.info(
                "livekit_agent_student_packet_received session_id=%s client_turn_id=%s source=%s",
                self.session_id, client_turn_id, source,
            )

            # Phase 4 (Step 3/6): once semantic turn CONTROL is genuinely
            # active for this session, a SPEECH-sourced browser student_text
            # is accepted for compatibility/diagnostics ONLY - it must never
            # independently trigger patient generation (that would race/
            # duplicate against the server-side Smart Turn decision for the
            # SAME utterance). An explicit MANUAL typed Send is a conscious
            # student action, not a race-prone recognizer guess, and is
            # deliberately exempted - see TurnSource.MANUAL_OVERRIDE. Re-
            # checked on every packet (not cached) so a mid-session fallback
            # (_fallback_to_browser_control) takes effect immediately for the
            # very next browser packet.
            semantic_ignored = self._semantic_control_active and not is_manual_override

            # Fix 1 (dual-engine guard): when the OpenAI Realtime engine is the
            # active turn/voice brain for this session, the raw student-AUDIO
            # path already drives every spoken turn in native voice. A browser
            # SpeechRecognition final must therefore NEVER also enter the legacy
            # _handle_student_turn/_run_turn/ElevenLabs pipeline - that would
            # double-respond and (on a key lacking text_to_speech) fail the turn
            # (the exact symptom this fixes). A deliberate MANUAL typed Send is
            # still honored, but routed through the SAME Realtime engine below,
            # never the legacy ElevenLabs path. Re-read per packet (not cached),
            # mirroring the semantic re-check above.
            realtime_settings = get_settings()
            realtime_active = realtime_settings.realtime_engine_active
            realtime_native = realtime_settings.realtime_native_agent_active
            realtime_ignored = realtime_active and not is_manual_override

            # Turn ACK is the FIRST action for any structurally valid packet -
            # before any dedup/processing decision, before OpenAI/TTS ever
            # starts (Part 3). A duplicate gets ack'd again too (Part 5). This
            # ack is unconditional even under Phase 4 semantic control below -
            # it only ever tells the browser "the agent received your
            # publish", never (by itself) "this will drive the conversation" -
            # the additive `semanticIgnored` flag is what tells a Phase-4-
            # aware frontend the difference, so acking a non-authoritative
            # browser packet cannot cause a duplicate patient response; it
            # only prevents a pointless client-side delivery-retry storm for
            # a packet the server is about to ignore.
            self._send_turn_ack(
                client_turn_id, semantic_ignored=semantic_ignored or realtime_ignored,
            )

            if semantic_ignored:
                self._log_agent_event(
                    "semantic_turn_browser_text_ignored", client_turn_id=client_turn_id,
                )
                logger.info(
                    "semantic_turn_browser_text_ignored session_id=%s client_turn_id=%s turn_source=%s "
                    "reason=semantic_control_active",
                    self.session_id, client_turn_id, TurnSource.BROWSER_TEXT.value,
                )
                return

            if realtime_ignored:
                # Browser speech-recognition text under the Realtime engine -
                # ack'd (so the browser doesn't retry) but never processed: the
                # audio path owns this spoken turn. No legacy _run_turn/ElevenLabs.
                self._log_agent_event(
                    "realtime_browser_text_ignored", client_turn_id=client_turn_id,
                )
                logger.info(
                    "realtime_browser_text_ignored session_id=%s client_turn_id=%s turn_source=%s "
                    "reason=realtime_engine_active",
                    self.session_id, client_turn_id, TurnSource.BROWSER_TEXT.value,
                )
                return

            if is_manual_override and self._semantic_control_active:
                self._log_agent_event(
                    "semantic_turn_manual_override_accepted", client_turn_id=client_turn_id,
                )
                logger.info(
                    "semantic_turn_manual_override_accepted session_id=%s client_turn_id=%s turn_source=%s",
                    self.session_id, client_turn_id, TurnSource.MANUAL_OVERRIDE.value,
                )

            if client_turn_id in self._completed_turn_ids or client_turn_id in self._in_flight_turn_ids:
                self._log_agent_event("livekit_agent_duplicate_turn_received", client_turn_id=client_turn_id)
                return

            # Fix 1: a MANUAL typed Send under the Realtime engine is a conscious
            # student action, so it IS honored - but through the SAME Realtime
            # pipeline (native voice), never the legacy _run_turn/ElevenLabs.
            # Reserve Realtime authority synchronously before scheduling, just
            # like an accepted spoken Realtime transcript.
            if realtime_active and is_manual_override:
                self._log_agent_event(
                    "realtime_manual_override_accepted", client_turn_id=client_turn_id,
                )
                if realtime_native:
                    realtime_session = self._realtime_session
                    if realtime_session is not None:
                        asyncio.ensure_future(
                            realtime_session.submit_typed_text(text, client_turn_id)
                        )
                else:
                    processing = self._accept_realtime_turn(client_turn_id, text)
                    if processing is not None:
                        asyncio.ensure_future(processing)
                return

            # Legacy path (Realtime engine off, or a semantic manual override):
            # reserve the slot synchronously (not inside the coroutine below) so
            # a duplicate arriving in the brief window before the scheduled task
            # actually starts running is still caught.
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

        # Phase D: match the outbound track rate to the active engine's audio
        # (Realtime native voice = 24kHz, legacy ElevenLabs = 16kHz). Computed
        # here from config (the engine is known at start() time), before the
        # track is published, so it is fixed for the job's lifetime.
        self._patient_audio_sample_rate = (
            REALTIME_PCM_SAMPLE_RATE if self._realtime_engine_active
            else patient_adapter.LIVEKIT_PCM_SAMPLE_RATE
        )
        # prompt_agent uses a small playout buffer so barge-in is not delayed by
        # ~1s of queued patient audio; all other engines keep LiveKit's default.
        audio_source_kwargs = {
            "sample_rate": self._patient_audio_sample_rate, "num_channels": 1,
        }
        if self._realtime_prompt_agent_active:
            audio_source_kwargs["queue_size_ms"] = _PROMPT_AGENT_AUDIO_QUEUE_MS
        self._audio_source = rtc.AudioSource(**audio_source_kwargs)
        track = rtc.LocalAudioTrack.create_audio_track("patient-voice", self._audio_source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions())
        logger.info("livekit_agent_track_published session_id=%s job_id=%s", self.session_id, self._job_id)

        loop = asyncio.get_running_loop()
        session_exists = await loop.run_in_executor(None, self._verify_session_exists)
        if not session_exists:
            logger.error(
                "livekit_agent_session_not_found_at_start session_id=%s job_id=%s", self.session_id, self._job_id,
            )
            await self._shutdown_and_signal("session_not_found")
            return

        # Phase 4 (Step 2): computed ONCE here, from config, before the
        # first agent_ready goes out - the frontend needs to know this
        # BEFORE it ever sees a browser SpeechRecognition final (Step 8).
        # A later runtime failure can still flip this False (one-way) via
        # _fallback_to_browser_control - see that method and Step 11.
        self._semantic_control_active = get_settings().semantic_turn_control_active
        logger.info(
            "semantic_turn_control_enabled session_id=%s active=%s",
            self.session_id, self._semantic_control_active,
        )
        # Phase 5A (Step 2): barge-in can never be active without turn
        # control also active - settings.semantic_barge_in_active already
        # encodes that dependency, but re-anding here is cheap, explicit,
        # and self-documenting at the exact point both flags are decided.
        self._semantic_barge_in_active = (
            self._semantic_control_active and get_settings().semantic_barge_in_active
        )
        logger.info(
            "semantic_barge_in_enabled session_id=%s active=%s",
            self.session_id, self._semantic_barge_in_active,
        )
        # Phase 5B (Step 2): independent of the two flags above - see
        # config.py's own note (the manual interrupt button already works
        # without either Phase 4/5A flag, so this fix is useful on its own).
        self._spoken_transcript_sync_active = get_settings().livekit_spoken_transcript_sync_enabled
        logger.info(
            "patient_spoken_transcript_sync_enabled session_id=%s active=%s",
            self.session_id, self._spoken_transcript_sync_active,
        )
        # Phase 6 (Step 2): requires turn control (like Phase 5A), NOT
        # barge-in - backchannel cancellation uses its own mechanism.
        self._backchannel_enabled = (
            self._semantic_control_active and get_settings().patient_backchannel_active
        )
        logger.info(
            "patient_backchannel_enabled session_id=%s active=%s",
            self.session_id, self._backchannel_enabled,
        )
        # Phase 7 (Step 2): requires turn control (like Phase 5A/6),
        # independent of barge-in/backchannel - resolution timers and
        # backchanneling are separate concerns that happen to both layer on
        # top of the same turn-CONTROL prerequisite.
        self._resolution_timers_enabled = (
            self._semantic_control_active and get_settings().semantic_resolution_timers_active
        )
        logger.info(
            "semantic_resolution_timers_enabled session_id=%s active=%s",
            self.session_id, self._resolution_timers_enabled,
        )

        if self._realtime_engine_active:
            realtime_session = await self._maybe_start_realtime_session(
                self._student_identity or "unattached", "unattached",
            )
            self._realtime_session_started.set()
            if realtime_session is None:
                await self._shutdown_and_signal("realtime_start_failed")
                return
            self._realtime_ready_task = asyncio.ensure_future(
                self._await_realtime_ready(realtime_session)
            )
        else:
            # Legacy readiness contract stays exactly as before.
            self._realtime_session_started.set()
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
        if self._agent_ready_sent:
            return
        self._agent_ready_sent = True
        elapsed_ms = (time.monotonic() - self._started_at) * 1000
        self._log_agent_event("livekit_agent_ready_sent", elapsed_ms=elapsed_ms)
        # Phase 4 (Step 7/8): additive field, backward-compatible with any
        # frontend build that doesn't read it (defaults to falsy/undefined
        # there, i.e. legacy browser-authoritative behavior).
        # `promptAgent` (additive, backward-compatible): tells the frontend that
        # OpenAI Realtime OWNS speech detection, turn-taking AND transcription
        # for this session, so the browser must NOT run its own SpeechRecognition
        # (it would be redundant dead weight and cause UI flicker). Absent/false
        # for legacy/controlled/native modes, which keep their existing behavior.
        self._publish_control({
            "type": "agent_ready",
            "semanticTurnControl": self._semantic_control_active,
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

    def _fallback_to_browser_control(self, reason: str) -> None:
        """Phase 4 (Step 11): ONE-WAY session-scoped downgrade - once called,
        _semantic_control_active stays False for the rest of this job/
        session (never flips back True, avoiding the oscillation Step 11
        explicitly forbids). Idempotent: a second/later call (e.g. the VAD
        stream AND the STT stream both dying around the same time) is a
        harmless no-op after the first. A no-op entirely if semantic control
        was never active in the first place (config off, or a session that
        never advertised it to the frontend) - never publishes a spurious
        semantic_fallback message a browser-authoritative session never
        asked about."""
        if not self._semantic_control_active:
            return
        self._semantic_control_active = False
        # Phase 5A: barge-in cannot outlive the turn-control pipeline it
        # depends on - cleared here too, unconditionally (harmless if it
        # was already False).
        self._semantic_barge_in_active = False
        # Phase 6: same - backchanneling cannot outlive turn control either,
        # and any pending/playing backchannel is cancelled immediately
        # (reuses the SAME cancellation path student-resumption uses).
        self._backchannel_enabled = False
        self._cancel_pending_backchannel()
        logger.info(
            "semantic_turn_fallback_to_browser session_id=%s reason=%s job_id=%s room_id=%s",
            self.session_id, reason, self._job_id, self._room_id,
        )
        # Tells the frontend to resume browser SpeechRecognition as
        # authoritative (Step 11/requirement 11) - reuses AGENT_CONTROL_TOPIC/
        # the same type-discriminated shape as agent_ready/turn_ack rather
        # than inventing a second protocol (Step 8).
        self._publish_control({"type": "semantic_fallback", "reason": reason})

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
        logger.info("livekit_agent_job_shutdown session_id=%s reason=%s", self.session_id, reason)
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
            self._native_agent_runtime = None
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

    async def _maybe_start_vad_stt_pipeline(
        self, identity: str, track_sid: str
    ) -> "_StudentVadSttPipeline | None":
        """Phase 2: returns a running _StudentVadSttPipeline, or None if the
        feature is off/unconfigured/fails to start - in EVERY None case the
        caller (_ingest_student_audio) simply continues exactly as it did in
        Phase 1, so an absent DEEPGRAM_API_KEY, an unset feature flag, the
        plugin packages not being installed, or a construction-time error are
        all equally safe, silent no-ops. Never raises."""
        settings = get_settings()
        if not (settings.livekit_server_stt_enabled and settings.deepgram_api_key):
            # Step 11: if this session's OWN config already told the
            # frontend semantic control was active (agent_ready), but the
            # underlying VAD/STT pipeline can't even start, that promise
            # can never be kept - fall back immediately so browser text
            # resumes being authoritative rather than the frontend sitting
            # in a semantic-control mode with nothing driving it.
            if self._semantic_control_active:
                self._fallback_to_browser_control("server_stt_unavailable")
            return None
        try:
            from livekit.plugins import deepgram, silero
        except Exception:
            logger.exception(
                "student_vad_stt_import_failed session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            if self._semantic_control_active:
                self._fallback_to_browser_control("vad_stt_import_failed")
            return None
        try:
            loop = asyncio.get_running_loop()
            # silero.VAD.load() is a blocking, one-time ONNX model load (the
            # SDK's own docs recommend a "prewarm" step) - run_in_executor
            # keeps that off the event loop that's also carrying LiveKit
            # room/data-channel I/O for this same job. Loaded fresh per
            # ingest task rather than cached at module/process level - this
            # deliberately matches the module docstring's "no module-level
            # mutable state shared across interviews" isolation discipline,
            # and the ~100ms one-time cost is negligible against an
            # interview's overall duration.
            vad = await loop.run_in_executor(None, silero.VAD.load)
            stt = deepgram.STT(
                api_key=settings.deepgram_api_key,
                interim_results=True,
                sample_rate=_VAD_STT_SAMPLE_RATE,
            )
        except Exception:
            logger.exception(
                "student_vad_stt_init_failed session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            if self._semantic_control_active:
                self._fallback_to_browser_control("vad_stt_init_failed")
            return None
        logger.info(
            "student_vad_stt_pipeline_started session_id=%s identity=%s track=%s",
            self.session_id, identity, track_sid,
        )
        candidate_turn = await self._maybe_start_turn_detector(identity, track_sid)
        return _StudentVadSttPipeline(
            session_id=self.session_id, identity=identity, track_sid=track_sid, vad=vad, stt=stt,
            candidate_turn=candidate_turn,
            # Phase 4 (Step 11): a courtesy signal only - a no-op unless
            # semantic control is genuinely active (see
            # _fallback_to_browser_control's own early-return).
            on_unhealthy=self._fallback_to_browser_control,
        )

    async def _maybe_start_turn_detector(
        self, identity: str, track_sid: str
    ) -> "_CandidateTurnCoordinator | None":
        """Phase 3 (EXPERIMENTAL): returns a running _CandidateTurnCoordinator
        wired to a SmartTurnDetector, or None if the feature is off/
        unconfigured/fails to start. ONLY called from
        _maybe_start_vad_stt_pipeline, i.e. only ever reached when Phase 2's
        VAD/STT pipeline itself already started successfully - semantic turn
        detection can never run without the VAD/STT events it depends on.
        Never raises - every failure mode here is caught, logged, and
        treated as 'stay off' (Step 10/11)."""
        settings = get_settings()
        if not settings.livekit_semantic_turn_detection_enabled:
            return None
        # Config-level inconsistency already logged once at Settings()
        # construction time (see config.py's
        # _warn_semantic_turn_detection_misconfig) - this is the per-job
        # enforcement of that same rule: caller only reaches this method
        # when livekit_server_stt_enabled is already True (see
        # _maybe_start_vad_stt_pipeline's own gate above), so in practice
        # this branch is defensive, not the primary warning path.
        if not settings.livekit_server_stt_enabled:
            logger.warning(
                "student_turn_detector_disabled_misconfig session_id=%s identity=%s track=%s "
                "reason=server_stt_disabled",
                self.session_id, identity, track_sid,
            )
            return None
        if not _SMART_TURN_MODEL_PATH.exists():
            logger.error(
                "student_turn_detector_model_missing session_id=%s identity=%s track=%s path=%s",
                self.session_id, identity, track_sid, _SMART_TURN_MODEL_PATH,
            )
            # Step 11: the detector this session's agent_ready already
            # promised the frontend can never come up (model file missing) -
            # fall back immediately rather than leaving the frontend
            # waiting on a semantic-turn-started that will never arrive.
            if self._semantic_control_active:
                self._fallback_to_browser_control("turn_detector_model_missing")
            return None
        try:
            loop = asyncio.get_running_loop()
            # Loading the ONNX session + WhisperFeatureExtractor is blocking
            # (same reasoning as silero.VAD.load() above) - keep it off the
            # event loop. Loaded fresh per ingest task, not cached at
            # module/process level - same isolation discipline as the
            # VAD/STT pipeline above.
            from app.livekit_agent.turn_detector import SmartTurnDetector

            detector = await loop.run_in_executor(
                None, lambda: SmartTurnDetector(model_path=str(_SMART_TURN_MODEL_PATH))
            )
        except Exception:
            logger.exception(
                "student_turn_detector_init_failed session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            if self._semantic_control_active:
                self._fallback_to_browser_control("turn_detector_init_failed")
            return None
        logger.info(
            "student_turn_detector_started session_id=%s identity=%s track=%s detector=%s",
            self.session_id, identity, track_sid, SmartTurnDetector.DETECTOR_ID,
        )
        # Phase 4 (Step 4/7): on_end/on_unhealthy are wired ONLY when
        # semantic control is genuinely active for this session - when it
        # isn't (Phase 3 default), the coordinator is constructed exactly
        # as before (on_end=None), so a HOLD/END decision only ever logs,
        # byte-for-byte the original observational-only behavior.
        semantic_control_active = self._semantic_control_active
        # Phase 5A (Step 2/4): is_patient_speaking/on_barge_in are wired
        # ONLY when barge-in is genuinely active for this session - when it
        # isn't, the coordinator behaves exactly as Phase 4 already did
        # (student speech during patient audio is still observed via VAD/
        # STT, same as always, but never classified or acted on).
        barge_in_active = self._semantic_barge_in_active
        # Phase 6 (Step 2/4): on_hold/is_backchannel_echo are wired ONLY
        # when backchanneling is genuinely active for this session - when
        # it isn't, HOLD decisions and STT finals behave exactly as Phase
        # 5B already did. on_student_resumed is wired WHENEVER backchannel
        # is active too - it is the cancellation signal, no reason to ever
        # omit it once backchannel scheduling itself can happen.
        backchannel_active = self._backchannel_enabled
        # Phase 7 (Step 2/4): resolution_timers_enabled is wired ONLY when
        # this session's flag is genuinely active (already AND'd with
        # semantic_control_active in start() - see _resolution_timers_enabled's
        # own note) - when it isn't, the coordinator's HOLD/END branches
        # behave byte-for-byte as they did before this phase.
        # on_before_commit is wired ONLY when backchanneling is ALSO active
        # (nothing to cancel otherwise) - reuses the SAME
        # _cancel_pending_backchannel function on_student_resumed already
        # uses, just invoked from a second, explicitly-named entry point
        # (a HOLD-recovery commit is not a "student resumed" event).
        resolution_timers_enabled = self._resolution_timers_enabled
        return _CandidateTurnCoordinator(
            session_id=self.session_id, identity=identity, track_sid=track_sid,
            detector=detector, sample_rate=_VAD_STT_SAMPLE_RATE,
            on_end=self._handle_semantic_turn_end if semantic_control_active else None,
            on_unhealthy=self._fallback_to_browser_control if semantic_control_active else None,
            is_patient_speaking=self._get_speaking_client_turn_id if barge_in_active else None,
            on_barge_in=self._on_semantic_barge_in if barge_in_active else None,
            on_hold=self._on_semantic_hold if backchannel_active else None,
            on_student_resumed=self._cancel_pending_backchannel if backchannel_active else None,
            is_backchannel_echo=self._is_likely_backchannel_echo if backchannel_active else None,
            resolution_timers_enabled=resolution_timers_enabled,
            on_before_commit=self._cancel_pending_backchannel if backchannel_active else None,
        )

    async def _ingest_student_audio(
        self,
        track: "rtc.Track",
        identity: str,
        track_sid: str,
        *,
        producer_generation: int | None = None,
    ) -> None:
        """Phase 1 proof-of-reach: continuously consumes the student's
        microphone frames and observes participant identity, track SID,
        sample rate, channel count, frame count, and elapsed audio duration -
        aggregated into one log line at most every
        _STUDENT_AUDIO_LOG_INTERVAL_SECONDS (never per-frame, never logging
        audio contents). Deliberately does NOT persist/save/print raw PCM or
        send audio to any generic destination.

        Phase 2 adds an OPTIONAL, PARALLEL VAD/STT pipeline fed the SAME
        frames (see _maybe_start_vad_stt_pipeline/_StudentVadSttPipeline) -
        purely diagnostic, never touches turn state. Phase 3 adds an
        OPTIONAL semantic turn detector on top of THAT (see
        _maybe_start_turn_detector/_CandidateTurnCoordinator) - same
        discipline. Any exception anywhere in any phase's processing is
        caught and logged, never propagated - this whole path runs entirely
        in parallel with, and can never break, the existing student_text
        turn pipeline."""
        import livekit.rtc as rtc

        # POC engine selection (default legacy). When the OpenAI Realtime engine
        # is active for this session it REPLACES the whole legacy VAD/STT/Smart
        # Turn stack (they are mutually exclusive turn-taking brains), and the
        # ingest stream is requested at Realtime's native 24kHz instead of the
        # legacy 16kHz - so exactly ONE consumer is fed and the audio is decoded
        # once at the rate that consumer needs. When Realtime is off (the
        # default), this is byte-for-byte the previous behavior: 16kHz mono into
        # the legacy _StudentVadSttPipeline.
        if self._realtime_engine_active:
            await self._realtime_session_started.wait()
            realtime_session = self._realtime_session
        else:
            realtime_session = None
        if self._realtime_engine_active and realtime_session is None:
            logger.error(
                "student_audio_realtime_unavailable session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            return
        if realtime_session is not None:
            ingest_sample_rate = realtime_session.input_sample_rate
            vad_stt_pipeline = None
        else:
            # Requests _VAD_STT_SAMPLE_RATE (16kHz) mono directly from LiveKit's
            # native FFI resampler at the SOURCE - Silero VAD, Deepgram STT, AND
            # (Phase 3) SmartTurnDetector all need exactly this rate, so doing
            # it ONCE here means none of them has to resample the same frame
            # again internally (each already tolerates a mismatched rate via
            # its own resampler - see _VAD_STT_SAMPLE_RATE's comment - so this
            # is purely a "do the one resample as early/once as possible"
            # optimization, not a behavior change).
            ingest_sample_rate = _VAD_STT_SAMPLE_RATE
            vad_stt_pipeline = await self._maybe_start_vad_stt_pipeline(identity, track_sid)
        stream = rtc.AudioStream(track, sample_rate=ingest_sample_rate, num_channels=1)
        if realtime_session is not None:
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
                if vad_stt_pipeline is not None:
                    vad_stt_pipeline.push_frame(frame)
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
            if vad_stt_pipeline is not None:
                await vad_stt_pipeline.aclose()
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
        """POC OpenAI Realtime engine (Phase A): returns a running, LISTEN-ONLY
        RealtimeSession, or None if the engine is off/unconfigured/fails to
        start. In EVERY None case the caller (_ingest_student_audio) falls back
        to the legacy VAD/STT path exactly as before, so an unset flag, a
        missing OPENAI_API_KEY, or a construction-time error are all equally
        safe, silent no-ops. Never raises (same fail-safe discipline as
        _maybe_start_vad_stt_pipeline)."""
        settings = get_settings()
        if not settings.realtime_engine_active:
            return None
        if self._realtime_session is not None:
            return self._realtime_session
        try:
            from app.livekit_agent.realtime_client import OpenAIRealtimeClient
            from app.livekit_agent.realtime_session import RealtimeSession

            # prompt_agent: OpenAI Realtime owns the whole conversation. Fully
            # isolated from controlled/native - it uses its own runtime, its own
            # per-patient model/voice/prompt, and bypasses turn control, backend
            # generation and native staging entirely.
            if settings.realtime_prompt_agent_active:
                return await self._start_prompt_agent_session(
                    settings, identity, track_sid,
                )

            native_runtime = None
            if settings.realtime_native_agent_active:
                from app.livekit_agent.native_agent_runtime import NativeRealtimeAgentRuntime

                native_runtime = NativeRealtimeAgentRuntime(
                    session_id=self.session_id,
                    case_id=self.case_id,
                    model_name=settings.openai_realtime_native_agent_model,
                    db_factory=self._session_factory,
                    reserve_generation=self._reserve_native_generation,
                    generation_is_current=lambda epoch: epoch == self._generation_epoch,
                    generation_authority=self._generation_authority_lock,
                    on_audio=self._publish_realtime_pcm,
                    on_speaking_started=self._on_native_speaking_started,
                    on_patient_final=self._on_native_patient_final,
                    on_student_persisted=self._on_native_student_persisted,
                    on_status=self._on_native_status,
                )
            client = OpenAIRealtimeClient(
                api_key=settings.openai_api_key,
                model=(
                    settings.openai_realtime_native_agent_model
                    if native_runtime is not None
                    else settings.openai_realtime_model
                ),
            )
            realtime_session = RealtimeSession(
                session_id=self.session_id, case_id=self.case_id, identity=identity,
                track_sid=track_sid, client=client, settings=settings,
                # Phase B: Realtime's own semantic_vad decides WHEN the student
                # turn is complete; the controller hands us exactly one
                # deduplicated (clientTurnId, transcript) per turn.
                on_turn_complete=(None if native_runtime is not None else self._accept_realtime_turn),
                # P0-3: raw speech boundaries control only candidate audio and
                # low-latency cutoff. Accepted transcription controls epochs.
                on_speech_started=self._on_realtime_speech_started,
                on_speech_stopped=self._on_realtime_speech_stopped,
                on_unavailable=self._on_realtime_unavailable,
                native_agent=native_runtime,
            )
            # Publish ownership before starting the task, so a duplicate call
            # cannot construct a second provider connection.
            self._realtime_session = realtime_session
            self._native_agent_runtime = native_runtime
            await realtime_session.start()
        except Exception:
            if self._realtime_session is locals().get("realtime_session"):
                self._realtime_session = None
                self._native_agent_runtime = None
            logger.exception(
                "realtime_session_start_failed session_id=%s identity=%s track=%s",
                self.session_id, identity, track_sid,
            )
            return None
        # Phase D: remember it so _run_realtime_turn can speak the approved
        # patient text through it (one Realtime session per interview).
        logger.info(
            "realtime_engine_active session_id=%s identity=%s track=%s",
            self.session_id, identity, track_sid,
        )
        return realtime_session

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
                # Realtime owns turn-taking AND authoring: no turn controller, no
                # backend turn acceptance. speech_started only clears the local
                # playback buffer on barge-in (OpenAI cancels its own response).
                on_turn_complete=None,
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
            "prompt_agent_engine_active session_id=%s case_id=%s identity=%s track=%s model=%s voice=%s",
            self.session_id, self.case_id, identity, track_sid,
            config["model"], config["voice"],
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

    def _reserve_native_generation(self, client_turn_id: str) -> int:
        """Native equivalent of P0-3's synchronous authoritative boundary."""
        self._in_flight_turn_ids.add(client_turn_id)
        with self._generation_authority_lock:
            self._generation_epoch += 1
            generation = self._generation_epoch
        speaking = self._speaking_client_turn_id
        if speaking is not None and speaking != client_turn_id:
            self._cutoff_realtime_patient_audio(speaking, reason="native_new_turn_accepted")
        self._log_agent_event("native_generation_reserved", client_turn_id=client_turn_id)
        return generation

    def _on_native_student_persisted(self, client_turn_id: str, epoch: int, text: str) -> None:
        self._send_transcript_sync(
            "student_transcript", client_turn_id, epoch=epoch, text=text,
        )

    def _on_native_speaking_started(self, client_turn_id: str, approved_text: str) -> None:
        self._speaking_client_turn_id = client_turn_id
        self._speaking_patient_text = approved_text
        self._realtime_cutoff_turn_id = None
        self._send_turn_status(client_turn_id, "speaking_started")

    def _on_native_patient_final(
        self, client_turn_id: str, epoch: int, persisted: "Any", reason: str,
    ) -> None:
        self._send_transcript_sync(
            "patient_text_ready",
            client_turn_id,
            epoch=epoch,
            text=persisted.patient_text,
            patient_turn_id=persisted.patient_turn_id,
        )
        self._send_transcript_sync(
            "patient_text_final",
            client_turn_id,
            epoch=epoch,
            text=persisted.patient_text,
            patient_turn_id=persisted.patient_turn_id,
            reason=reason,
        )

    def _on_native_status(self, client_turn_id: str, status: str) -> None:
        self._send_turn_status(client_turn_id, status)
        if status in ("speaking_ended", "interrupted", "failed"):
            if self._speaking_client_turn_id == client_turn_id:
                self._speaking_client_turn_id = None
                self._speaking_patient_text = None
                self._realtime_cutoff_turn_id = None
            self._in_flight_turn_ids.discard(client_turn_id)
            self._mark_turn_completed(client_turn_id)

    def _on_realtime_speech_started(self) -> None:
        """Low-latency audio signal, never authoritative turn evidence."""
        if self._realtime_student_speech_active:
            self._log_agent_event(
                "realtime_candidate_speech_duplicate",
                client_turn_id=self._speaking_client_turn_id or self._active_client_turn_id or "",
            )
            return
        self._realtime_student_speech_active = True
        self._realtime_student_speech_stopped.clear()
        speaking = self._speaking_client_turn_id
        active = self._active_client_turn_id
        self._log_agent_event(
            "realtime_candidate_speech_started", client_turn_id=speaking or active or "",
        )
        if speaking is not None:
            self._cutoff_realtime_patient_audio(speaking, reason="candidate_speech_started")

    def _on_realtime_speech_stopped(self) -> None:
        """Release pre-speech waiters when actual student audio stops."""
        if not self._realtime_student_speech_active:
            return
        self._realtime_student_speech_active = False
        self._realtime_student_speech_stopped.set()
        self._log_agent_event("realtime_candidate_speech_stopped")

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

    def _cutoff_realtime_patient_audio(self, client_turn_id: str, *, reason: str) -> None:
        """Immediately and idempotently cut one actually-speaking Realtime turn."""
        if (
            not client_turn_id
            or self._speaking_client_turn_id != client_turn_id
            or self._realtime_cutoff_turn_id == client_turn_id
        ):
            return
        self._realtime_cutoff_turn_id = client_turn_id
        # Native mode must quarantine the response synchronously. The provider
        # cancel is awaited by a scheduled task below, but the receive loop may
        # already have another audio delta buffered before that task runs.
        if self._realtime_native_agent_active and self._realtime_session is not None:
            self._realtime_session.quarantine_active_native_response()
        if self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                logger.exception("realtime_barge_in_clear_queue_failed session_id=%s", self.session_id)
        if self._realtime_session is not None:
            asyncio.ensure_future(self._realtime_session.cancel_active_response())
        self._log_agent_event("realtime_patient_audio_cutoff", client_turn_id=client_turn_id)
        logger.info(
            "realtime_patient_audio_cutoff session_id=%s client_turn_id=%s reason=%s",
            self.session_id, client_turn_id, reason,
        )

    def _accept_realtime_turn(self, client_turn_id: str, transcript: str) -> "Awaitable[None] | None":
        """Reserve generation synchronously, then return unscheduled work."""
        if not transcript.strip():
            self._log_agent_event("realtime_empty_turn_ignored", client_turn_id=client_turn_id)
            return None
        if client_turn_id in self._completed_turn_ids or client_turn_id in self._in_flight_turn_ids:
            self._log_agent_event("realtime_duplicate_turn_received", client_turn_id=client_turn_id)
            return None
        self._in_flight_turn_ids.add(client_turn_id)
        with self._generation_authority_lock:
            self._generation_epoch += 1
            my_generation = self._generation_epoch
        self._log_agent_event("realtime_generation_reserved", client_turn_id=client_turn_id)
        logger.info(
            "realtime_generation_reserved session_id=%s client_turn_id=%s generation=%d",
            self.session_id, client_turn_id, my_generation,
        )
        # Close the generating->speaking race: A may have begun speaking after
        # B's raw speech_started but before B's transcript became authoritative.
        speaking = self._speaking_client_turn_id
        if speaking is not None and speaking != client_turn_id:
            self._cutoff_realtime_patient_audio(speaking, reason="new_generation_accepted")
        return self._handle_realtime_turn(
            client_turn_id, transcript, my_generation=my_generation, reserved=True,
        )

    async def _handle_realtime_turn(
        self,
        client_turn_id: str,
        transcript: str,
        my_generation: int | None = None,
        *,
        reserved: bool = False,
    ) -> None:
        """The ONE entry point a completed Realtime student turn reaches the
        backend through (see RealtimeTurnController - it already guarantees
        exactly-once, non-empty delivery with a stable, unique clientTurnId).

        A genuinely new student utterance must NEVER be dropped just because
        an older patient response is still generating. Its generation is
        reserved synchronously by _accept_realtime_turn before this coroutine
        is scheduled, so B/C ownership cannot be reordered by task scheduling.
        A direct call is retained as a test/internal compatibility entry point
        and routes through that same reservation seam exactly once."""
        if not reserved:
            processing = self._accept_realtime_turn(client_turn_id, transcript)
            if processing is not None:
                await processing
            return
        if my_generation is None:
            raise RuntimeError("reserved Realtime turn is missing its generation")
        self._log_agent_event("realtime_student_turn_received", client_turn_id=client_turn_id)
        logger.info(
            "realtime_student_turn_received session_id=%s client_turn_id=%s transcript=%r "
            "turn_source=%s",
            self.session_id, client_turn_id, transcript, TurnSource.SERVER_SEMANTIC.value,
        )
        try:
            async with self._turn_lock:
                if my_generation != self._generation_epoch:
                    # A newer utterance superseded us while we waited for the
                    # lock - abandon before doing anything (never a stale
                    # generation, never stale audio). Marked completed so the
                    # id is never reprocessed.
                    logger.info(
                        "realtime_turn_superseded_before_start session_id=%s client_turn_id=%s "
                        "my_generation=%d current_generation=%d",
                        self.session_id, client_turn_id, my_generation, self._generation_epoch,
                    )
                    self._mark_turn_completed(client_turn_id)
                    return
                self._active_turn_task = asyncio.current_task()
                self._active_client_turn_id = client_turn_id
                try:
                    await self._run_realtime_turn(transcript, client_turn_id, my_generation)
                finally:
                    if self._active_client_turn_id == client_turn_id:
                        self._active_turn_task = None
                        self._active_client_turn_id = None
        finally:
            self._in_flight_turn_ids.discard(client_turn_id)

    async def _run_realtime_turn(self, text: str, client_turn_id: str, my_epoch: int) -> None:
        """The Realtime engine's OWN turn runner - separate from the legacy
        _run_turn (ElevenLabs), which stays byte-for-byte untouched. Runs the
        EXISTING authoritative patient pipeline (generation -> validation ->
        disclosure gating -> persistence, via generate_and_persist_turn, ZERO
        bypass), then speaks the APPROVED text in native voice (Phase D).

        Phase F guards, all keyed on `my_epoch` vs the live generation epoch:
          - The generation-epoch predicate is passed INTO
            generate_and_persist_turn, so if this turn was superseded during
            its (uncancellable) OpenAI call, GenerationStaleError is raised
            BEFORE any DB/disclosure write - nothing is persisted.
          - A pre-speak re-check covers the narrow window between the persist
            gate and speaking; if superseded there, the persisted row is
            finalized as not-delivered and native voice never starts.
          - An interrupt DURING native speech (Case 1) returns interrupted from
            speak(); the row is finalized to only the portion actually voiced."""
        loop = asyncio.get_running_loop()
        self._log_agent_event("realtime_turn_processing_started", client_turn_id=client_turn_id)
        result = None
        try:
            try:
                result = await loop.run_in_executor(
                    None, self._generate_realtime_turn_sync, text, client_turn_id,
                    lambda: my_epoch == self._generation_epoch,
                )
            except patient_adapter.GenerationStaleError:
                # Case 2: superseded while generating - nothing was persisted,
                # disclosure untouched. Abandon silently: no speak, no status,
                # no frontend speaking state (the newer turn drives instead).
                logger.info(
                    "realtime_turn_stale_before_persist session_id=%s client_turn_id=%s my_epoch=%d",
                    self.session_id, client_turn_id, my_epoch,
                )
                return
            logger.info(
                "realtime_patient_response_approved session_id=%s client_turn_id=%s "
                "patient_turn_id=%s voice_key=%s generated_char_count=%d replayed=%s",
                self.session_id, client_turn_id, result.patient_turn_id, result.voice_key,
                len(result.patient_text), result.replayed,
            )
            # Phase G: the student turn is now persisted (generate_and_persist_turn
            # wrote both rows before returning), so surface the authoritative
            # student text. Emitted even for a turn about to be found stale below
            # so the visible transcript matches the DB (the student DID say it).
            self._send_transcript_sync(
                "student_transcript", client_turn_id, epoch=my_epoch, text=text,
            )
            if self._realtime_session is None:
                logger.error(
                    "realtime_turn_no_session_to_speak session_id=%s client_turn_id=%s",
                    self.session_id, client_turn_id,
                )
                self._send_turn_status(client_turn_id, "failed")
                return
            # Raw speech is non-authoritative, but Carly must not begin over
            # an active continuation. Every teardown path sets this event, so
            # a stopped session cannot strand a task at this boundary.
            await self._realtime_student_speech_stopped.wait()
            # Pre-speak stale check: a new utterance may have arrived in the
            # tiny window after the persist gate passed. Do NOT speak; the row
            # is persisted but Carly never voiced it, so finalize it as
            # not-delivered (the DB must not imply audio the student never heard).
            if my_epoch != self._generation_epoch:
                logger.info(
                    "realtime_turn_stale_before_speak session_id=%s client_turn_id=%s my_epoch=%d "
                    "current_epoch=%d",
                    self.session_id, client_turn_id, my_epoch, self._generation_epoch,
                )
                await self._finalize_realtime_partial(result, "", client_turn_id, reason="interrupted")
                return
            # Phase G: approved patient text goes out BEFORE speech starts, so
            # the frontend can render it immediately rather than waiting for
            # audio to (nearly) finish. Carries patientTurnId + epoch for
            # correlation/stale-drop.
            self._send_transcript_sync(
                "patient_text_ready", client_turn_id, epoch=my_epoch,
                text=result.patient_text, patient_turn_id=result.patient_turn_id,
            )
            self._send_turn_status(client_turn_id, "speaking_started")
            # Marks the ONLY window in which a barge-in cancels native audio
            # (see _on_realtime_speech_started), mirroring legacy _run_turn.
            self._speaking_client_turn_id = client_turn_id
            self._speaking_patient_text = result.patient_text
            self._realtime_cutoff_turn_id = None
            speak_result = await self._realtime_session.speak(
                client_turn_id=client_turn_id, text=result.patient_text,
                on_audio=self._publish_realtime_pcm,
            )
            # Verbatim-fidelity check (Phase D): what Realtime actually SPOKE
            # vs the backend-approved text - a material divergence is logged as
            # a candidate blocker, never silently accepted.
            self._check_voice_fidelity(client_turn_id, result.patient_text, speak_result.spoken_transcript)
            if speak_result.interrupted:
                # Case 1: finalize to only the portion actually voiced, so the
                # transcript never claims Carly said text the student never heard,
                # then reconcile the frontend to that SAME delivered portion.
                await self._finalize_realtime_partial(
                    result, speak_result.spoken_transcript, client_turn_id, reason="interrupted",
                )
                self._send_transcript_sync(
                    "patient_text_final", client_turn_id, epoch=my_epoch,
                    text=speak_result.spoken_transcript.strip(),
                    patient_turn_id=result.patient_turn_id, reason="interrupted",
                )
                self._send_turn_status(client_turn_id, "interrupted")
            elif speak_result.completed:
                # Normal completion: DB content == approved text; confirm the
                # final authoritative content to the frontend uniformly.
                self._send_transcript_sync(
                    "patient_text_final", client_turn_id, epoch=my_epoch,
                    text=result.patient_text, patient_turn_id=result.patient_turn_id,
                    reason="complete",
                )
                self._send_turn_status(client_turn_id, "speaking_ended")
            else:
                await self._finalize_realtime_partial(
                    result, speak_result.spoken_transcript, client_turn_id, reason="delivery_failed",
                )
                self._send_transcript_sync(
                    "patient_text_final", client_turn_id, epoch=my_epoch,
                    text=speak_result.spoken_transcript.strip(),
                    patient_turn_id=result.patient_turn_id, reason="delivery_failed",
                )
                self._send_turn_status(client_turn_id, "failed")
            logger.info(
                "realtime_turn_spoken session_id=%s client_turn_id=%s audio_bytes=%d completed=%s "
                "interrupted=%s",
                self.session_id, client_turn_id, speak_result.audio_bytes, speak_result.completed,
                speak_result.interrupted,
            )
        except patient_adapter.LiveKitPocSessionNotFoundError:
            logger.error(
                "realtime_turn_session_not_found session_id=%s client_turn_id=%s",
                self.session_id, client_turn_id,
            )
        except Exception:
            logger.exception(
                "realtime_turn_generation_failed session_id=%s client_turn_id=%s job_id=%s room_id=%s",
                self.session_id, client_turn_id, self._job_id, self._room_id,
            )
            self._log_agent_event("realtime_turn_processing_failed", client_turn_id=client_turn_id)
        finally:
            if self._speaking_client_turn_id == client_turn_id:
                self._speaking_client_turn_id = None
                self._speaking_patient_text = None
                self._realtime_cutoff_turn_id = None
            self._mark_turn_completed(client_turn_id)

    def _generate_realtime_turn_sync(
        self, text: str, client_turn_id: str, is_generation_valid: "Callable[[], bool]"
    ) -> "patient_adapter.PocTurnResult":
        """Realtime-only executor body: the SAME generate_and_persist_turn the
        legacy path uses, plus the Phase F generation-epoch predicate that gates
        persistence/disclosure. Kept separate from the legacy _generate_turn_sync
        so that path's signature/behavior is completely unchanged."""
        db = self._session_factory()
        try:
            return patient_adapter.generate_and_persist_turn(
                db, session_id=self.session_id, case_id=self.case_id,
                question=text, client_turn_id=client_turn_id,
                is_generation_valid=is_generation_valid,
                generation_authority=self._generation_authority_lock,
            )
        finally:
            db.close()

    async def _finalize_realtime_partial(
        self, result: "patient_adapter.PocTurnResult", spoken_text: str,
        client_turn_id: str, *, reason: str,
    ) -> None:
        """Reconcile a persisted patient row down to only the text whose native
        audio genuinely reached the student (empty when interrupted before any
        audio). Reuses the EXISTING _finalize_partial_patient_delivery_sync /
        patient_adapter.finalize_partial_patient_delivery path - the same
        transcript semantics the legacy interruption flow uses - so assessment
        reads a truthful record."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._finalize_partial_patient_delivery_sync,
            result.patient_turn_id, spoken_text.strip(), reason,
        )
        logger.info(
            "realtime_turn_finalized_partial session_id=%s client_turn_id=%s patient_turn_id=%s "
            "reason=%s generated_char_count=%d delivered_char_count=%d",
            self.session_id, client_turn_id, result.patient_turn_id, reason,
            len(result.patient_text), len(spoken_text.strip()),
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

    def _check_voice_fidelity(self, client_turn_id: str, approved: str, spoken: str) -> None:
        """Phase D verbatim-fidelity check: does the native voice's OWN
        transcript match the backend-approved text? Compared on a normalized
        (lowercased, punctuation/whitespace-stripped) basis so trivial STT
        punctuation differences don't false-alarm. A real divergence is logged
        as a WARNING (a candidate blocker per the Phase D requirement) - never
        silently ignored, but also never used to fabricate/alter the persisted
        transcript (the DB row remains the backend-approved text)."""
        if not spoken:
            logger.warning(
                "realtime_voice_fidelity_no_transcript session_id=%s client_turn_id=%s",
                self.session_id, client_turn_id,
            )
            return
        norm_approved = _normalize_for_fidelity(approved)
        norm_spoken = _normalize_for_fidelity(spoken)
        if norm_approved == norm_spoken:
            logger.info(
                "realtime_voice_fidelity_ok session_id=%s client_turn_id=%s", self.session_id, client_turn_id,
            )
        else:
            logger.warning(
                "realtime_voice_fidelity_mismatch session_id=%s client_turn_id=%s "
                "approved=%r spoken=%r",
                self.session_id, client_turn_id, approved, spoken,
            )

    def _get_speaking_client_turn_id(self) -> str | None:
        """Phase 5A: the `is_patient_speaking` callback wired into
        _CandidateTurnCoordinator (see _maybe_start_turn_detector) - a
        thin, explicit getter rather than handing the coordinator a raw
        reference to `self`, keeping it as narrowly-scoped as `on_end`/
        `on_unhealthy` already are."""
        return self._speaking_client_turn_id

    async def _on_semantic_barge_in(
        self, speaking_client_turn_id: str, transcript: str, new_candidate_turn_id: str
    ) -> None:
        """Phase 5A (Step 7): the ONLY place a TRUE_BARGE_IN classification
        can stop patient audio - wired as _CandidateTurnCoordinator's
        `on_barge_in` callback, fired fire-and-forget the instant the
        coordinator's OWN buffer classification confirms it (see
        turn_detector.classify_barge_in). Reuses the EXACT SAME
        cancellation primitive the manual interrupt_patient control message
        uses (_cancel_active_patient_turn - Step 12) - there is only one
        cancellation code path, not two. The coordinator has ALREADY
        promoted `new_candidate_turn_id`/`transcript` into its own normal
        candidate-turn state (fresh id, seeded segments) before calling
        this - Smart Turn's ordinary HOLD/END machinery picks up from there
        (Step 7.8-7.11) using the EXISTING Phase 4 _handle_semantic_turn_end
        path; this method's only job is the audio-side interruption."""
        if not self._semantic_barge_in_active:
            # Step 11/13: a fallback (or the flag simply never being active)
            # landed between the coordinator's classification and this
            # callback actually running - never act on a stale wiring.
            self._log_agent_event(
                "semantic_barge_in_duplicate_ignored", client_turn_id=speaking_client_turn_id,
            )
            return

        logger.info(
            "semantic_barge_in_confirmed session_id=%s patient_client_turn_id=%s "
            "student_candidate_turn_id=%s transcript=%r",
            self.session_id, speaking_client_turn_id, new_candidate_turn_id, transcript,
        )

        # Step 13 (diagnostic-only, never suppresses): flag when the
        # barge-in transcript overlaps heavily with the patient's OWN
        # current words - a possible sign of the patient's TTS audio being
        # picked up by the student's own mic rather than genuine speech.
        # Word-overlap, not a hard gate - see the class/module notes on why
        # a real echo-cancellation fix is out of scope for this phase.
        speaking_patient_text = self._speaking_patient_text
        if speaking_patient_text:
            barge_words = set(normalize_barge_in_text(transcript).split())
            patient_words = set(normalize_barge_in_text(speaking_patient_text).split())
            if barge_words and len(barge_words & patient_words) / len(barge_words) >= 0.6:
                logger.info(
                    "semantic_barge_in_possible_echo session_id=%s patient_client_turn_id=%s "
                    "student_candidate_turn_id=%s",
                    self.session_id, speaking_client_turn_id, new_candidate_turn_id,
                )

        start = time.monotonic()
        logger.info(
            "semantic_barge_in_cancel_started session_id=%s patient_client_turn_id=%s",
            self.session_id, speaking_client_turn_id,
        )
        cancelled = self._cancel_active_patient_turn(speaking_client_turn_id, reason="semantic_barge_in")
        elapsed_ms = (time.monotonic() - start) * 1000
        if cancelled:
            logger.info(
                "semantic_barge_in_cancel_complete session_id=%s patient_client_turn_id=%s "
                "cancellation_latency_ms=%.1f",
                self.session_id, speaking_client_turn_id, elapsed_ms,
            )
        else:
            # Step 10 race: the patient turn already resolved (naturally
            # finished, failed, or was already interrupted by the manual
            # button) between the coordinator's classification and this
            # callback running - a safe no-op. The student's barge-in
            # transcript is NOT lost: the coordinator already promoted it
            # into a normal candidate turn regardless of this outcome, so
            # Smart Turn's own END pipeline still submits it once ready.
            logger.info(
                "semantic_barge_in_race_speaking_already_ended session_id=%s "
                "patient_client_turn_id=%s",
                self.session_id, speaking_client_turn_id,
            )

    # --- Phase 6 (EXPERIMENTAL patient backchanneling) --------------------

    def _backchannel_eligible(self, candidate_turn_id: str) -> bool:
        """Phase 6 (Step 4): session-STATE eligibility - separate from
        _CandidateTurnCoordinator's own content pre-filter (word count),
        which already ran before on_hold was ever called. Re-checked at
        EVERY decision point (scheduling, after the delay, immediately
        before publish) since state can change during any await in
        between - including from WITHIN _schedule_backchannel/
        _play_backchannel's own re-checks, which run AS
        self._backchannel_task itself; the `is asyncio.current_task()`
        comparison is what lets a task correctly re-check its OWN
        eligibility without spuriously seeing itself as "another backchannel
        already scheduled/playing"."""
        task = self._backchannel_task
        no_other_backchannel_active = task is None or task.done() or task is asyncio.current_task()
        return (
            self._backchannel_enabled
            and not self._shutdown_called
            and self._active_client_turn_id is None  # no real patient turn thinking/speaking (Step 4)
            and self._backchannel_played_turn_id != candidate_turn_id  # Step 8: max one per turn
            and no_other_backchannel_active
        )

    def _on_semantic_hold(self, candidate_turn_id: str, transcript: str, probability: float | None) -> None:
        """Phase 6 (Step 3/4): wired as _CandidateTurnCoordinator's on_hold
        callback (sync - only ever kicks off a background task here; never
        awaited by the coordinator itself). A PATIENT_BACKCHANNEL decision,
        NEVER a PATIENT_RESPONSE one - this method and everything it
        schedules never calls _handle_student_turn/_run_turn, never touches
        _turn_lock/_completed_turn_ids/_active_client_turn_id, never
        persists anything (Step 17/19)."""
        if not self._backchannel_eligible(candidate_turn_id):
            return
        probability_str = f"{probability:.4f}" if probability is not None else "-"
        logger.info(
            "patient_backchannel_eligible session_id=%s semantic_turn_id=%s probability=%s delay_ms=%.0f "
            "candidate_text=%r",
            self.session_id, candidate_turn_id, probability_str, _BACKCHANNEL_HOLD_DELAY_SECONDS * 1000, transcript,
        )
        logger.info(
            "patient_backchannel_scheduled session_id=%s semantic_turn_id=%s", self.session_id, candidate_turn_id,
        )
        self._backchannel_task = asyncio.ensure_future(self._schedule_backchannel(candidate_turn_id))

    async def _schedule_backchannel(self, candidate_turn_id: str) -> None:
        """Phase 6 (Step 5/6/20): waits the post-HOLD delay, then plays -
        cancelled at any point by _cancel_pending_backchannel (student
        resumed, or session falling back to browser control). Fail-open:
        ANY exception here is caught, logged, and treated as "silently
        skip" - a backchannel is optional polish, it must never affect the
        student's semantic turn (Step 20)."""
        try:
            await asyncio.sleep(_BACKCHANNEL_HOLD_DELAY_SECONDS)
            if not self._backchannel_eligible(candidate_turn_id):
                return
            await self._play_backchannel(candidate_turn_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "patient_backchannel_failed session_id=%s semantic_turn_id=%s", self.session_id, candidate_turn_id,
            )
        finally:
            if self._backchannel_task is asyncio.current_task():
                self._backchannel_task = None
                self._backchannel_playing = False

    async def _play_backchannel(self, candidate_turn_id: str) -> None:
        """Phase 6 (Step 9/10/11/12): resolves voice (Step 11: skips rather
        than ever guessing wrong), synthesizes (cache-first - Step 10),
        publishes via the EXISTING _publish_pcm. Deliberately never sets
        _speaking_client_turn_id/_speaking_patient_text (Step 3: those mean
        a REAL patient response) and never calls _send_turn_status (Step
        12/18: no ordinary patient turn lifecycle for a backchannel - raw
        audio already flows through the existing persistent track
        regardless of any status message, so the frontend hears it without
        needing to know anything changed)."""
        if self._audio_source is None:
            return
        voice_key = patient_adapter.resolve_backchannel_voice_key(self.case_id)
        if voice_key is None:
            logger.info(
                "patient_backchannel_skipped_voice_unavailable session_id=%s semantic_turn_id=%s",
                self.session_id, candidate_turn_id,
            )
            return

        phrase = _BACKCHANNEL_PHRASES[0]
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(
            None,
            lambda: patient_adapter.synthesize_backchannel_audio_pcm(
                case_id=self.case_id, voice_key=voice_key, phrase=phrase,
            ),
        )
        if pcm is None:
            logger.info(
                "patient_backchannel_failed session_id=%s semantic_turn_id=%s reason=synthesis_failed",
                self.session_id, candidate_turn_id,
            )
            return

        # Re-check ONE more time - state may have changed while TTS/cache
        # lookup was in flight (Step 4/6).
        if not self._backchannel_eligible(candidate_turn_id):
            logger.info(
                "patient_backchannel_cancelled_student_resumed session_id=%s semantic_turn_id=%s reason=late",
                self.session_id, candidate_turn_id,
            )
            return

        self._backchannel_played_turn_id = candidate_turn_id
        self._backchannel_playing = True
        clip_duration_s = len(pcm) / 2 / patient_adapter.LIVEKIT_PCM_SAMPLE_RATE
        self._backchannel_echo_guard = (
            normalize_barge_in_text(phrase),
            time.monotonic() + clip_duration_s + _BACKCHANNEL_ECHO_GRACE_SECONDS,
        )
        logger.info(
            "patient_backchannel_started session_id=%s semantic_turn_id=%s voice_key=%s phrase=%r",
            self.session_id, candidate_turn_id, voice_key, phrase,
        )
        start = time.monotonic()
        try:
            await self._publish_pcm(pcm)
        except asyncio.CancelledError:
            logger.info(
                "patient_backchannel_audio_cancelled session_id=%s semantic_turn_id=%s playback_duration_ms=%.0f",
                self.session_id, candidate_turn_id, (time.monotonic() - start) * 1000,
            )
            raise
        else:
            logger.info(
                "patient_backchannel_completed session_id=%s semantic_turn_id=%s playback_duration_ms=%.0f",
                self.session_id, candidate_turn_id, (time.monotonic() - start) * 1000,
            )
        finally:
            self._backchannel_playing = False

    def _cancel_pending_backchannel(self) -> None:
        """Phase 6 (Step 6/7): the ONE cancellation entry point - called
        from _CandidateTurnCoordinator's on_student_resumed (student
        started speaking again) and from _fallback_to_browser_control. A
        harmless no-op if nothing is scheduled/playing. Deliberately does
        NOT send patient_turn_status "interrupted" and does NOT invoke
        Phase 5B's transcript finalization - a backchannel was never a real
        patient turn, so cancelling one has none of those side effects
        (Step 18/19)."""
        task = self._backchannel_task
        if task is None or task.done():
            return
        if self._backchannel_playing and self._audio_source is not None:
            try:
                self._audio_source.clear_queue()
            except Exception:
                logger.exception("patient_backchannel_clear_queue_failed session_id=%s", self.session_id)
        logger.info("patient_backchannel_cancelled_student_resumed session_id=%s", self.session_id)
        task.cancel()

    def _is_likely_backchannel_echo(self, text: str) -> bool:
        """Phase 6 (Step 13): conservative, EXACT-match-only check (never a
        fuzzy/substring heuristic that could discard real student content)
        - wired as _CandidateTurnCoordinator's is_backchannel_echo callback.
        Backchannel phrases are a small, known, fixed set, which is what
        makes exact-match discarding safe here - unlike Phase 5A's own
        echo DIAGNOSTIC for arbitrary, unpredictable real patient text
        (which only ever logs a hint, never discards)."""
        guard = self._backchannel_echo_guard
        if guard is None:
            return False
        normalized_phrase, expiry = guard
        if time.monotonic() > expiry:
            self._backchannel_echo_guard = None
            return False
        return normalize_barge_in_text(text) == normalized_phrase

    async def _handle_semantic_turn_end(self, candidate_turn_id: str, transcript: str) -> None:
        """Phase 4 (Step 4): the ONLY place a Smart Turn END decision can
        turn into a real patient turn - wired as _CandidateTurnCoordinator's
        `on_end` callback (see _maybe_start_turn_detector), fired
        fire-and-forget from _evaluate_boundary. Submits into the EXACT
        SAME turn lock / patient-generation / persistence / TTS / audio-
        publish / patient_turn_status pipeline browser student_text uses
        (_handle_student_turn/_run_turn) - there is only one canonical
        patient-response pipeline; this method does not duplicate any of
        it, only decides WHETHER and WITH WHAT TEXT to call into it."""
        if not self._semantic_control_active:
            # Step 11: a fallback happened between this boundary evaluating
            # END and this callback actually running (both are async) - the
            # session is now browser-authoritative; never submit a
            # server-originated turn after that point.
            self._log_agent_event(
                "semantic_turn_end_ignored_after_fallback", client_turn_id=candidate_turn_id,
            )
            return

        if candidate_turn_id in self._completed_turn_ids or candidate_turn_id in self._in_flight_turn_ids:
            # Should be unreachable in practice - _CandidateTurnCoordinator
            # hands out a fresh id per candidate turn and resets before
            # firing this callback (see _evaluate_boundary) - kept as a
            # cheap, explicit backstop matching the required
            # "duplicate END cannot create two patient responses" guarantee.
            self._log_agent_event(
                "semantic_turn_duplicate_prevented", client_turn_id=candidate_turn_id,
            )
            logger.info(
                "semantic_turn_duplicate_prevented session_id=%s semantic_turn_id=%s turn_source=%s",
                self.session_id, candidate_turn_id, TurnSource.SERVER_SEMANTIC.value,
            )
            return

        logger.info(
            "semantic_turn_submitted session_id=%s semantic_turn_id=%s turn_source=%s candidate_text=%r",
            self.session_id, candidate_turn_id, TurnSource.SERVER_SEMANTIC.value, transcript,
        )
        self._log_agent_event("semantic_turn_submitted", client_turn_id=candidate_turn_id)

        # Step 8: tells the frontend a server-originated turn is now
        # processing (thinking) - the ONE new message type this phase adds;
        # everything downstream (speaking_started/speaking_ended/failed)
        # reuses the EXISTING patient_turn_status protocol unchanged, keyed
        # by this SAME candidate_turn_id.
        self._publish_control({"type": "semantic_turn_started", "clientTurnId": candidate_turn_id})

        # Reserved synchronously, exactly like the browser path in _on_data,
        # so this method's own dedup check above is race-free with a
        # (structurally near-impossible, but cheap to guard) concurrent call.
        self._in_flight_turn_ids.add(candidate_turn_id)
        await self._handle_student_turn(transcript, candidate_turn_id)

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

        # Phase 5B: populated only under the per-sentence branch below -
        # declared here (not inside the try block) so the except
        # CancelledError branch can always safely reference them, even if
        # cancellation somehow raced in before either was ever touched
        # (e.g. during _generate_turn_sync itself).
        result: "patient_adapter.PocTurnResult | None" = None
        spoken_sentences: list[str] = []

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

            if self._spoken_transcript_sync_active:
                # Phase 5B: publishes sentence-by-sentence and finalizes its
                # own "failed" path internally (mid-response TTS failure) -
                # see the method's own docstring. False means it already
                # handled everything (status sent, transcript corrected);
                # _run_turn must not fall through to normal-completion logic.
                delivered_fully = await self._run_turn_sentence_by_sentence(
                    result, client_turn_id, on_stage, stages, spoken_sentences,
                )
                if not delivered_fully:
                    return
            else:
                # Unchanged from before Phase 5B - one whole-response TTS
                # call, one publish. Byte-for-byte identical when the flag
                # is off (the default).
                pcm = await loop.run_in_executor(
                    None,
                    # voice_key was resolved once, alongside speaker/response
                    # generation, in generate_and_persist_turn (see
                    # patient_adapter.py's speaker-routing parity note) - passed
                    # through here rather than re-derived, so TTS always
                    # synthesizes the SAME participant's voice that answered.
                    lambda: patient_adapter.synthesize_patient_audio_pcm(
                        case_id=self.case_id, text=result.patient_text,
                        voice_key=result.voice_key, on_stage=on_stage,
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
                # Phase 5A (Step 13, diagnostic-only): the patient's own text for
                # this turn, so a later barge-in candidate can be compared
                # against it (semantic_barge_in_possible_echo) - never used to
                # suppress a real interruption, only to log a hint.
                self._speaking_patient_text = result.patient_text
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
            # Phase 5B (Step 9/10): the SAME finalization primitive manual
            # interrupt and semantic barge-in both end up here through -
            # this except block is reached regardless of WHICH cancellation
            # source triggered task.cancel() (see _cancel_active_patient_
            # turn, the one place that ever does). `result` is None only if
            # cancellation raced in before generation even finished, in
            # which case generate_and_persist_turn's own atomic insert never
            # ran either - nothing to correct.
            if self._spoken_transcript_sync_active and result is not None:
                await self._finalize_partial_patient_delivery(
                    result, spoken_sentences, client_turn_id=client_turn_id, reason="interrupted",
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
            # Phase 5B (Step 12): covers a genuine (non-Cancelled) exception
            # raised AFTER generation already persisted the full text - e.g.
            # _publish_pcm itself raising mid-sentence, distinct from
            # _run_turn_sentence_by_sentence's own OWN "pcm is None" TTS-
            # failure handling (which already finalizes internally before
            # this branch is ever reached). `result is None` here (OpenAI/
            # generation itself failed) correctly skips this - nothing was
            # ever persisted for _generate_turn_sync to have corrected.
            if self._spoken_transcript_sync_active and result is not None:
                await self._finalize_partial_patient_delivery(
                    result, spoken_sentences, client_turn_id=client_turn_id, reason="delivery_failed",
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
                self._speaking_patient_text = None
            # Marked completed (success, failure, OR interrupt) regardless of
            # which branch above ran, EXCEPT when this method wasn't entered
            # at all (busy-drop, handled in _handle_student_turn) - a
            # duplicate of this clientTurnId must never trigger a second
            # OpenAI/TTS call again after this point. Idempotent if
            # _on_interrupt_patient already called this for the same id.
            self._mark_turn_completed(client_turn_id)

    async def _run_turn_sentence_by_sentence(
        self,
        result: "patient_adapter.PocTurnResult",
        client_turn_id: str,
        on_stage,
        stages: list[tuple[str, float]],
        spoken_sentences: list[str],
    ) -> bool:
        """Phase 5B (Step 2/5): publishes patient audio ONE SENTENCE AT A
        TIME instead of one whole-response blob (see
        patient_adapter.split_into_sentences), so each sentence gets a
        genuine, provable "did this sentence's audio finish publishing"
        boundary - never a timer-based guess. Appends to the CALLER's
        `spoken_sentences` list directly, so a CancelledError raised mid-
        `_publish_pcm` for the CURRENT sentence (propagating up to
        _run_turn's own except block) leaves it holding only PREVIOUSLY,
        fully-published sentences (Step 6: never claim the interrupted
        sentence was spoken).

        Returns False when a mid-response TTS failure occurs (status
        already sent, transcript already corrected internally, exactly
        mirroring the flag-off path's own `pcm is None` handling) - the
        caller must return immediately rather than fall through to
        normal-completion logic. Returns True once every sentence has
        published successfully."""
        loop = asyncio.get_running_loop()
        sentences = patient_adapter.split_into_sentences(result.patient_text)
        total_bytes = 0
        logger.info(
            "patient_transcript_pending session_id=%s client_turn_id=%s patient_turn_id=%s "
            "generated_char_count=%d sentence_count=%d",
            self.session_id, client_turn_id, result.patient_turn_id,
            len(result.patient_text), len(sentences),
        )
        for index, sentence in enumerate(sentences):
            logger.info(
                "patient_spoken_unit_started session_id=%s client_turn_id=%s unit_index=%d unit_char_count=%d",
                self.session_id, client_turn_id, index, len(sentence),
            )
            pcm = await loop.run_in_executor(
                None,
                lambda s=sentence: patient_adapter.synthesize_patient_audio_pcm(
                    case_id=self.case_id, text=s, voice_key=result.voice_key, on_stage=on_stage,
                ),
            )
            if pcm is None:
                logger.error(
                    "livekit_agent_tts_failed session_id=%s client_turn_id=%s unit_index=%d",
                    self.session_id, client_turn_id, index,
                )
                await self._finalize_partial_patient_delivery(
                    result, spoken_sentences, client_turn_id=client_turn_id, reason="delivery_failed",
                )
                self._send_turn_status(client_turn_id, "failed")
                self._log_turn_timing(client_turn_id, stages)
                return False

            if self._speaking_client_turn_id is None:
                # First sentence about to publish - announce ONCE for the
                # whole turn (Step 7: reuse the existing protocol exactly,
                # no new frontend state - a student never sees a
                # "speaking_started" per sentence).
                on_stage("first_audio_publish_start")
                self._send_turn_status(client_turn_id, "speaking_started")
                # Phase D2: marks the ONLY window in which
                # _cancel_active_patient_turn will actually cancel this task.
                self._speaking_client_turn_id = client_turn_id
                # Phase 5A (Step 13, diagnostic-only): see the flag-off
                # path's identical comment.
                self._speaking_patient_text = result.patient_text

            await self._publish_pcm(pcm)  # raises CancelledError here if interrupted mid-sentence
            spoken_sentences.append(sentence)
            total_bytes += len(pcm)
            logger.info(
                "patient_spoken_unit_committed session_id=%s client_turn_id=%s unit_index=%d unit_char_count=%d",
                self.session_id, client_turn_id, index, len(sentence),
            )

        on_stage("speech_complete")
        self._send_turn_status(client_turn_id, "speaking_ended")
        self._log_turn_timing(client_turn_id, stages)
        logger.info(
            "livekit_agent_turn_audio_published session_id=%s client_turn_id=%s bytes=%d",
            self.session_id, client_turn_id, total_bytes,
        )
        # No DB write needed here - generate_and_persist_turn's original
        # full-text insert is already correct since every sentence was
        # genuinely delivered. This log is the only proof of that.
        logger.info(
            "patient_transcript_finalized_full session_id=%s client_turn_id=%s patient_turn_id=%s "
            "committed_char_count=%d",
            self.session_id, client_turn_id, result.patient_turn_id, len(result.patient_text),
        )
        return True

    async def _finalize_partial_patient_delivery(
        self,
        result: "patient_adapter.PocTurnResult",
        spoken_sentences: list[str],
        *,
        client_turn_id: str,
        reason: str,
    ) -> None:
        """Phase 5B (Step 9/10/13): the ONE finalization primitive - reached
        from _run_turn's except CancelledError branch (manual interrupt AND
        semantic barge-in both cancel through the SAME
        _cancel_active_patient_turn, so there is no separate code path per
        interruption source - Step 10) and from
        _run_turn_sentence_by_sentence's own mid-response TTS-failure
        handling (Step 12). Structurally reachable at most ONCE per
        patient_turn_id (both call sites are on _run_turn's single,
        already-serialized control-flow path) - the dedup guard below is a
        defensive backstop (Step 13), not load-bearing today."""
        patient_turn_id = result.patient_turn_id
        if patient_turn_id in self._finalized_patient_turn_ids:
            self._log_agent_event(
                "patient_transcript_finalize_duplicate_ignored", client_turn_id=client_turn_id,
            )
            return
        self._finalized_patient_turn_ids[patient_turn_id] = None
        self._finalized_patient_turn_ids.move_to_end(patient_turn_id)
        while len(self._finalized_patient_turn_ids) > _MAX_COMPLETED_TURN_IDS:
            self._finalized_patient_turn_ids.popitem(last=False)

        spoken_text = " ".join(spoken_sentences).strip()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._finalize_partial_patient_delivery_sync, patient_turn_id, spoken_text, reason,
        )
        logger.info(
            "patient_transcript_finalized_interrupted session_id=%s client_turn_id=%s patient_turn_id=%s "
            "reason=%s generated_char_count=%d committed_char_count=%d",
            self.session_id, client_turn_id, patient_turn_id, reason,
            len(result.patient_text), len(spoken_text),
        )

    def _finalize_partial_patient_delivery_sync(
        self, patient_turn_id: str, spoken_text: str, reason: str
    ) -> None:
        db = self._session_factory()
        try:
            patient_adapter.finalize_partial_patient_delivery(
                db, patient_turn_id=patient_turn_id, spoken_text=spoken_text, reason=reason,
            )
        finally:
            db.close()

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
        "livekit_agent_job_connected job_id=%s session_id=%s case_id=%s room=%s",
        ctx.job.id, session_id, case_id, ctx.room.name,
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
