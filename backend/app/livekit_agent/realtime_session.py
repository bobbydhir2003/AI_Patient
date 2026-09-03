"""Phase A: one OpenAI Realtime session per interview, LISTEN-ONLY.

Scope of THIS phase (deliberately minimal - see the approved POC plan):
  - open exactly one Realtime connection for one interview,
  - configure it as the turn-taking brain (semantic_vad) with
    create_response=false / interrupt_response=false so it NEVER invents or
    speaks a patient answer,
  - forward the student's microphone PCM to it,
  - observe and LOG the turn-taking signals (speech_started / speech_stopped /
    input_audio_buffer.committed / input transcription completed) and errors.

It does NOT (yet) call patient_engine, create a backend turn, publish audio, or
touch DB/transcript state - those arrive in Phases B-D. This class holds NO
reference to PocAgentSession's turn-driving state, mirroring the isolation
discipline of the legacy _CandidateTurnCoordinator (worker.py): a failure
anywhere here is caught and logged and can never break the student's audio
ingest task, let alone the legacy conversation path.

Provider specifics live entirely behind the injected `client` (see
realtime_client.OpenAIRealtimeClient) so this orchestration is unit-testable
with a fake connection and no network.
"""
from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.core.logging import get_logger
from app.livekit_agent.realtime_client import (
    REALTIME_PCM_SAMPLE_RATE,
    RealtimeConnectionLike,
    build_native_agent_session_update,
    build_prompt_agent_session_update,
    build_session_update,
    encode_audio_append,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger("app.livekit_agent.realtime")

# Bounds the outbound audio queue so a slow/stalled connection can never grow
# memory unboundedly - a student mic produces ~50 frames/sec, so this is a few
# seconds of buffered audio at most. On overflow the OLDEST frame is dropped
# (audio in-flight to a stalled socket is already stale), never the newest.
_MAX_QUEUED_AUDIO_FRAMES = 200

# Realtime server event type strings (GA schema, openai>=1.107) we act on.
# Everything else is counted and logged at debug only.
_EVT_SESSION_CREATED = "session.created"
_EVT_SESSION_UPDATED = "session.updated"
_EVT_SPEECH_STARTED = "input_audio_buffer.speech_started"
_EVT_SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
_EVT_COMMITTED = "input_audio_buffer.committed"
_EVT_TRANSCRIPTION_DONE = "conversation.item.input_audio_transcription.completed"
_EVT_ERROR = "error"

# Phase D: outbound (patient-speech) response lifecycle - the events a
# response.create produces once the backend asks Realtime to speak approved
# text in native voice (GA schema, verified against openai 1.109.1's
# response_audio_delta_event / response_audio_transcript_done_event types).
_EVT_RESPONSE_AUDIO_DELTA = "response.output_audio.delta"
_EVT_RESPONSE_AUDIO_DONE = "response.output_audio.done"
_EVT_RESPONSE_TRANSCRIPT_DELTA = "response.output_audio_transcript.delta"
_EVT_RESPONSE_TRANSCRIPT_DONE = "response.output_audio_transcript.done"
_EVT_RESPONSE_DONE = "response.done"

# Bounded wait for a single patient response to finish speaking before speak()
# gives up (defensive - a well-behaved response ends well within this).
_RESPONSE_TIMEOUT_SECONDS = 45.0


@dataclass
class SpeakResult:
    """Outcome of one speak() call - carries the ACTUAL transcript Realtime
    produced for its own audio, so the caller can compare it against the
    backend-approved text (the Phase D verbatim-fidelity check)."""

    spoken_transcript: str
    audio_bytes: int
    completed: bool
    interrupted: bool


# on_audio(pcm24_bytes) - awaited per audio delta so the caller (PocAgentSession)
# can stream frames straight into its LiveKit AudioSource as they arrive.
AudioSink = Callable[[bytes], Awaitable[None]]

# Optional observer hook (event_type, event) - used by tests to react to
# turn-taking signals. The hook is additive and defaults to None.
EventHook = Callable[[str, Any], None]
UnavailableHook = Callable[[str], None]


class RealtimeSession:
    """Owns ONE interview's Realtime connection lifecycle. Constructed per job,
    never shared across interviews (same isolation contract as PocAgentSession).

    Lifecycle: start() launches a single background task that connects, sends
    the session.update config, then runs an audio-sender loop and an
    event-receive loop concurrently until aclose() (or a fatal connection
    error). push_audio_bytes() is a cheap, non-blocking enqueue safe to call
    from the audio ingest loop.
    """

    input_sample_rate = REALTIME_PCM_SAMPLE_RATE

    def __init__(
        self,
        *,
        session_id: str,
        case_id: str,
        identity: str,
        track_sid: str,
        client: Any,
        settings: "Settings",
        on_event: EventHook | None = None,
        on_turn_complete: "Callable[[str, str], Any] | None" = None,
        on_speech_started: "Callable[[], None] | None" = None,
        on_speech_stopped: "Callable[[], None] | None" = None,
        on_unavailable: UnavailableHook | None = None,
        native_agent: Any | None = None,
        prompt_agent: Any | None = None,
    ) -> None:
        self._session_id = session_id
        self._case_id = case_id
        self._identity = identity
        self._track_sid = track_sid
        self._client = client
        self._settings = settings
        self._on_event = on_event
        # Phase E/F: invoked on every input speech_started (barge-in / new-
        # utterance signal). None keeps the session non-interrupting.
        self._on_speech_started = on_speech_started
        self._on_speech_stopped = on_speech_stopped
        self._on_unavailable = on_unavailable
        self._native_agent = native_agent
        if self._native_agent is not None:
            self._native_agent.bind_session(self)
        # prompt_agent mode: Realtime OWNS the conversation. Mutually exclusive
        # with native_agent (the worker only ever wires one). When set, this
        # session sends the prompt_agent session.update and routes every server
        # event to the runtime, which handles audio-out + transcript persistence.
        self._prompt_agent = prompt_agent
        if self._prompt_agent is not None:
            self._prompt_agent.bind_session(self)
        self._native_followups: dict[str, dict[str, Any]] = {}
        self._native_active_response_id: str | None = None
        self._native_active_item_id: str | None = None
        self._native_audio_started_at: float | None = None
        self._native_audio_bytes = 0
        self._native_cancel_requested: set[str] = set()
        self._native_cancel_sent: set[str] = set()
        # Phase B: when supplied, a RealtimeTurnController turns the raw event
        # stream into exactly one deduplicated backend turn per completed
        # utterance and invokes this callback with (client_turn_id, transcript).
        # None (the Phase A default) keeps this session pure listen-only.
        self._turn_controller = None
        if on_turn_complete is not None:
            from app.livekit_agent.realtime_turn_controller import RealtimeTurnController

            self._turn_controller = RealtimeTurnController(
                session_id=session_id, on_turn_complete=on_turn_complete,
            )

        self._audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=_MAX_QUEUED_AUDIO_FRAMES)
        # Phase D: the single in-flight patient response's event sink. Set only
        # while speak() is awaiting a response; the receive loop routes
        # response.* events onto it. One at a time (PocAgentSession's _turn_lock
        # already serializes patient turns), so a single-slot queue is enough.
        self._active_response: "asyncio.Queue[tuple[str, Any]] | None" = None
        self._active_response_turn: str | None = None
        # Phase E/F: the Realtime response_id of the in-flight patient response,
        # learned from response.created. Used to reject LATE audio/transcript
        # events from an already-cancelled response so they can never be
        # misrouted into a newer one (stale-audio protection).
        self._active_response_id: str | None = None
        # Set on the first local interruption for this response, before any
        # await. This makes duplicate interruption signals a true no-op even
        # after the known response_id has been consumed for cancellation.
        self._active_response_cancel_requested = False
        # response.done may be routed just before a local interruption task
        # runs. In that ordering the response is already terminal and there is
        # nothing left to cancel (avoids a harmless but noisy provider error).
        self._active_response_terminal = False
        # A local cutoff can beat response.created. Preserve that unmatched
        # create as an orphan so it is cancelled/drained before a later turn
        # can arm its PCM sink; otherwise late A audio could be mistaken for B.
        self._active_response_cancelled_before_created = False
        self._orphan_response_creates = 0
        self._orphan_response_ids: set[str] = set()
        self._orphan_responses_drained = asyncio.Event()
        self._orphan_responses_drained.set()
        self._conn: RealtimeConnectionLike | None = None
        self._run_task: "asyncio.Task[None] | None" = None
        # `session.update` being written to the socket is not readiness. The
        # provider's matching `session.updated` event is the authoritative
        # acknowledgement that the effective configuration was accepted.
        self._configured_ready = asyncio.Event()
        # Backward-compatible private alias for existing focused tests. Its
        # semantics are intentionally strengthened: it now means configured,
        # not merely "session.update was sent".
        self._ready = self._configured_ready
        self._ready_or_terminated = asyncio.Event()
        self._connected = False
        self._configuration_pending = False
        self._terminated = False
        self._failed = False
        self._close_reason: str | None = None
        self._close_requested = False
        self._closed = False

        # Lightweight in-memory counters, logged once as a summary on aclose -
        # enough to judge the POC (frames reached Realtime, which turn-taking
        # signals fired), not a monitoring platform.
        self._frames_sent = 0
        self._frames_dropped = 0
        self._event_counts: dict[str, int] = {}

    async def start(self) -> None:
        """Launches the background connection task and returns immediately - the
        caller (the audio ingest loop) must not block waiting for the WebSocket
        handshake. Audio pushed before the connection is ready is buffered."""
        if self._run_task is not None:
            return
        self._run_task = asyncio.ensure_future(self._run())
        logger.info(
            "realtime_session_starting session_id=%s identity=%s track=%s model=%s voice=%s",
            self._session_id, self._identity, self._track_sid,
            (
                self._prompt_agent.config["model"]
                if self._prompt_agent is not None
                else self._settings.openai_realtime_native_agent_model
                if self._native_agent is not None
                else self._settings.openai_realtime_model
            ),
            (
                self._prompt_agent.config["voice"]
                if self._prompt_agent is not None
                else self._settings.openai_realtime_voice
            ),
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._terminated

    @property
    def is_ready(self) -> bool:
        return (
            self._connected
            and self._configured_ready.is_set()
            and not self._terminated
            and not self._closed
        )

    @property
    def is_closed(self) -> bool:
        return self._closed or self._terminated

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def close_reason(self) -> str | None:
        return self._close_reason

    async def wait_until_ready(self, timeout: float) -> bool:
        """Wait boundedly for `session.updated` or terminal failure."""
        if self.is_ready:
            return True
        if self._terminated:
            return False
        try:
            await asyncio.wait_for(self._ready_or_terminated.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.is_ready

    def push_audio_bytes(self, pcm16: bytes) -> None:
        """Enqueue one frame of 24kHz mono PCM16 student audio for delivery to
        Realtime. Non-blocking and never raises: if the queue is full (stalled
        connection) the oldest frame is dropped so live ingest is never
        back-pressured or interrupted."""
        if not self.is_ready or not pcm16:
            if pcm16:
                self._frames_dropped += 1
            return
        try:
            self._audio_queue.put_nowait(pcm16)
        except asyncio.QueueFull:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(pcm16)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self._frames_dropped += 1

    async def _run(self) -> None:
        """Connect, configure, then pump audio out and events in concurrently.
        Any failure is logged and ends the session cleanly - it never escapes
        into the ingest task that owns this object."""
        try:
            async with self._client.connect() as conn:
                self._conn = conn
                self._connected = True
                self._configuration_pending = True
                if self._prompt_agent is not None:
                    session_update = build_prompt_agent_session_update(
                        self._settings, self._prompt_agent.config,
                    )
                elif self._native_agent is not None:
                    session_update = build_native_agent_session_update(
                        self._settings,
                        instructions=self._native_agent.instructions,
                    )
                else:
                    session_update = build_session_update(self._settings)
                await conn.send(session_update)
                logger.info(
                    "realtime_session_connected_config_pending session_id=%s identity=%s track=%s",
                    self._session_id, self._identity, self._track_sid,
                )
                sender = asyncio.ensure_future(self._sender_loop(conn))
                receiver = asyncio.ensure_future(self._receiver_loop(conn))
                try:
                    done, _pending = await asyncio.wait(
                        (sender, receiver), return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        # Surface a real loop exception into the outer handler.
                        task.result()
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed = True
            self._close_reason = "provider_connection_failed"
            logger.exception(
                "realtime_session_run_failed session_id=%s identity=%s track=%s",
                self._session_id, self._identity, self._track_sid,
            )
        finally:
            self._connected = False
            self._configuration_pending = False
            self._configured_ready.clear()
            self._terminated = True
            if self._close_reason is None:
                self._close_reason = "client_closed" if self._close_requested else "provider_connection_closed"
            if not self._close_requested:
                self._failed = True
                logger.error(
                    "realtime_session_unavailable session_id=%s reason=%s",
                    self._session_id, self._close_reason,
                )
                if self._on_unavailable is not None:
                    try:
                        self._on_unavailable(self._close_reason)
                    except Exception:
                        logger.exception(
                            "realtime_session_unavailable_hook_failed session_id=%s",
                            self._session_id,
                        )
            self._ready_or_terminated.set()

    async def _sender_loop(self, conn: RealtimeConnectionLike) -> None:
        while not self._closed:
            pcm16 = await self._audio_queue.get()
            try:
                await conn.send(encode_audio_append(pcm16))
                self._frames_sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "realtime_session_audio_send_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
                return

    async def _receiver_loop(self, conn: RealtimeConnectionLike) -> None:
        while not self._closed:
            try:
                event = await conn.recv()
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                return
            except Exception:
                logger.exception(
                    "realtime_session_recv_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
                return
            if event is None:
                return
            self._handle_event(event)
            if self._prompt_agent is not None:
                event_type = getattr(event, "type", None) or (
                    event.get("type") if isinstance(event, dict) else None
                )
                if event_type:
                    await self._prompt_agent.handle_event(event_type, event)
            if self._native_agent is not None:
                event_type = getattr(event, "type", None) or (
                    event.get("type") if isinstance(event, dict) else None
                )
                if event_type:
                    await self._native_agent.handle_event(event_type, event)
                    if event_type == _EVT_RESPONSE_DONE:
                        response = _get(event, "response")
                        response_id = _get(response, "id") or _get(event, "response_id")
                        followup = self._native_followups.pop(str(response_id or ""), None)
                        if followup is not None and self.is_ready and self._conn is conn:
                            await conn.send(followup)

    def _handle_event(self, event: Any) -> None:
        event_type = getattr(event, "type", None) or (
            event.get("type") if isinstance(event, dict) else None
        )
        if not event_type:
            return
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

        # Phase D: route an in-flight patient response's audio/transcript/done
        # events (and any error raised during it) to the waiting speak() call.
        # Kept FIRST so response.* events are never mistaken for input
        # turn-taking signals.
        if (event_type.startswith("response.") or event_type == _EVT_ERROR) and (
            self._active_response is not None
            or self._orphan_response_creates
            or self._orphan_response_ids
        ):
            self._route_response_event(event_type, event)
            return

        if event_type == _EVT_SPEECH_STARTED:
            logger.info(
                "realtime_student_speech_started session_id=%s identity=%s track=%s",
                self._session_id, self._identity, self._track_sid,
            )
            # Phase E/F barge-in / new-utterance signal - fired BEFORE the turn
            # controller so the backend can invalidate/cancel an in-flight
            # patient turn at the earliest possible moment. Isolated so a
            # callback error never breaks the receive loop.
            if self._on_speech_started is not None:
                try:
                    self._on_speech_started()
                except Exception:
                    logger.exception(
                        "realtime_on_speech_started_failed session_id=%s", self._session_id,
                    )
        elif event_type == _EVT_SPEECH_STOPPED:
            logger.info(
                "realtime_student_speech_stopped session_id=%s identity=%s track=%s",
                self._session_id, self._identity, self._track_sid,
            )
            if self._on_speech_stopped is not None:
                try:
                    self._on_speech_stopped()
                except Exception:
                    logger.exception(
                        "realtime_on_speech_stopped_failed session_id=%s", self._session_id,
                    )
        elif event_type == _EVT_COMMITTED:
            logger.info(
                "realtime_student_turn_committed session_id=%s identity=%s track=%s item_id=%s",
                self._session_id, self._identity, self._track_sid,
                _get(event, "item_id") or "-",
            )
        elif event_type == _EVT_TRANSCRIPTION_DONE:
            transcript = _get(event, "transcript") or ""
            logger.info(
                "realtime_student_transcription session_id=%s identity=%s track=%s item_id=%s transcript=%r",
                self._session_id, self._identity, self._track_sid,
                _get(event, "item_id") or "-", transcript,
            )
        elif event_type == _EVT_SESSION_CREATED or event_type == _EVT_SESSION_UPDATED:
            logger.info(
                "realtime_session_event session_id=%s track=%s event=%s",
                self._session_id, self._track_sid, event_type,
            )
            if event_type == _EVT_SESSION_UPDATED:
                effective_session = _get(event, "session")
                effective_type = _get(effective_session, "type") if effective_session is not None else None
                if (
                    self._connected
                    and self._configuration_pending
                    and effective_session is not None
                    and effective_type in (None, "realtime")
                    and self._effective_configuration_valid(effective_session)
                ):
                    self._configuration_pending = False
                    self._configured_ready.set()
                    self._ready_or_terminated.set()
                    logger.info(
                        "realtime_session_configured_ready session_id=%s track=%s",
                        self._session_id, self._track_sid,
                    )
                else:
                    logger.warning(
                        "realtime_session_updated_ignored session_id=%s track=%s pending=%s connected=%s",
                        self._session_id, self._track_sid,
                        self._configuration_pending, self._connected,
                    )
        elif event_type == _EVT_ERROR:
            logger.error(
                "realtime_session_error session_id=%s track=%s error=%s",
                self._session_id, self._track_sid, _get(event, "error") or event,
            )
        else:
            logger.debug(
                "realtime_session_event_other session_id=%s track=%s event=%s",
                self._session_id, self._track_sid, event_type,
            )

        # Phase B: feed the turn controller (when active) so it can assemble
        # exactly one backend turn per completed utterance. Isolated inside the
        # controller's own try/except - never breaks the receive loop.
        if self._turn_controller is not None:
            self._turn_controller.handle_event(event_type, event)

        # Additive observer hook (tests). Never allowed to break the receive loop.
        if self._on_event is not None:
            try:
                self._on_event(event_type, event)
            except Exception:
                logger.exception(
                    "realtime_session_event_hook_failed session_id=%s event=%s",
                    self._session_id, event_type,
                )

    def _effective_configuration_valid(self, effective_session: Any) -> bool:
        """Native readiness additionally proves both authority tools survived."""
        if self._native_agent is None:
            return True
        from app.livekit_agent.realtime_client import (
            NATIVE_ALLOWED_FACTS_TOOL,
            NATIVE_STAGE_RESPONSE_TOOL,
        )

        tools = _get(effective_session, "tools") or []
        names = {_get(tool, "name") for tool in tools}
        required = {NATIVE_ALLOWED_FACTS_TOOL, NATIVE_STAGE_RESPONSE_TOOL}
        if required.issubset(names):
            return True
        logger.error(
            "native_session_tools_not_accepted session_id=%s accepted_tools=%s",
            self._session_id, sorted(str(name) for name in names if name),
        )
        return False

    def _route_response_event(self, event_type: str, event: Any) -> None:
        """Feed one outbound-response event to the active speak() collector.
        Runs in the (sync) receive loop, so it only ever enqueues - the async
        publishing/awaiting happens in speak().

        Phase E/F stale-audio protection: learn the response_id from
        response.created and REJECT any later audio/transcript event whose
        response_id does not match - a late delta from an already-cancelled
        response can never be published into (or misattributed to) a newer one."""
        if event_type == "response.created":
            resp = _get(event, "response")
            response_id = _get(resp, "id") if resp is not None else None
            if self._orphan_response_creates:
                self._orphan_response_creates -= 1
                if response_id:
                    self._orphan_response_ids.add(response_id)
                    asyncio.ensure_future(self._cancel_orphan_response(response_id))
                elif not self._orphan_response_creates and not self._orphan_response_ids:
                    # A response.created without an id cannot be targeted or
                    # correlated. Do not strand the next response forever.
                    self._orphan_responses_drained.set()
                return
            self._active_response_id = response_id
            return
        response_id = _get(event, "response_id")
        if response_id is None:
            resp = _get(event, "response")
            response_id = _get(resp, "id") if resp is not None else None
        if event_type == _EVT_ERROR and (
            self._orphan_response_creates or self._orphan_response_ids
        ):
            # A targeted cancel may race completion and legitimately produce a
            # provider error. It must not make the session unusable. Keep any
            # known cancelled ids for stale-event rejection, but release the
            # drain boundary so a newer response can proceed.
            logger.error(
                "realtime_response_cancel_error session_id=%s error=%s",
                self._session_id, _get(event, "error") or event,
            )
            self._orphan_response_creates = 0
            self._orphan_responses_drained.set()
            if self._active_response is None:
                return
        if response_id in self._orphan_response_ids:
            if event_type == _EVT_RESPONSE_DONE:
                self._orphan_response_ids.discard(response_id)
                if not self._orphan_response_creates and not self._orphan_response_ids:
                    self._orphan_responses_drained.set()
            return
        queue = self._active_response
        if queue is None:
            return
        # Once we know the active response_id, drop any event carrying a
        # different one (a straggler from a cancelled response).
        if self._active_response_id is not None:
            if response_id is not None and response_id != self._active_response_id:
                logger.info(
                    "realtime_stale_response_event_ignored session_id=%s event=%s stale_response_id=%s",
                    self._session_id, event_type, response_id,
                )
                return
        if event_type == _EVT_RESPONSE_AUDIO_DELTA:
            delta = _get(event, "delta")
            if delta:
                try:
                    pcm = base64.b64decode(delta)
                except Exception:
                    logger.exception("realtime_response_audio_decode_failed session_id=%s", self._session_id)
                    return
                queue.put_nowait(("audio", pcm))
        elif event_type == _EVT_RESPONSE_TRANSCRIPT_DELTA:
            queue.put_nowait(("transcript_delta", _get(event, "delta") or ""))
        elif event_type == _EVT_RESPONSE_TRANSCRIPT_DONE:
            queue.put_nowait(("transcript_done", _get(event, "transcript") or ""))
        elif event_type == _EVT_RESPONSE_DONE:
            self._active_response_terminal = True
            queue.put_nowait(("done", None))
        elif event_type == _EVT_ERROR:
            queue.put_nowait(("error", _get(event, "error")))

    async def send_event(self, event: dict[str, Any]) -> None:
        """Send one native-agent client event through this job-owned socket."""
        if not self.is_ready or self._conn is None:
            raise RuntimeError("Realtime session is unavailable")
        await self._conn.send(event)

    async def send_tool_output(
        self,
        *,
        call_id: str,
        output: str,
        after_response_id: str,
        followup: dict[str, Any] | None,
    ) -> None:
        """Return a function result now; start its follow-up after response.done.

        A default Realtime conversation permits one response at a time. Waiting
        for the tool-call response's terminal event prevents overlapping model
        responses while still appending the tool result immediately.
        """
        await self.send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        })
        if followup is not None:
            self._native_followups[after_response_id] = followup

    async def submit_typed_text(self, text: str, client_turn_id: str) -> None:
        if self._native_agent is None:
            raise RuntimeError("typed native input requires native-agent mode")
        await self._native_agent.submit_typed_text(text, client_turn_id)

    def arm_native_response(self, response_id: str) -> None:
        if response_id:
            self._native_active_response_id = response_id
            self._native_active_item_id = None
            self._native_audio_started_at = None
            self._native_audio_bytes = 0

    def note_native_audio(self, response_id: str, item_id: str, byte_count: int) -> None:
        if response_id != self._native_active_response_id or byte_count <= 0:
            return
        if item_id:
            self._native_active_item_id = item_id
        if self._native_audio_started_at is None:
            self._native_audio_started_at = time.monotonic()
        self._native_audio_bytes += byte_count

    def disarm_native_response(self, response_id: str) -> None:
        if self._native_active_response_id == response_id:
            self._native_active_response_id = None
            self._native_active_item_id = None
            self._native_audio_started_at = None
            self._native_audio_bytes = 0

    def is_native_response_cancelled(self, response_id: str) -> bool:
        return bool(response_id and response_id in self._native_cancel_requested)

    def quarantine_active_native_response(self) -> str | None:
        """Synchronously reject late PCM before the async cancel task runs."""
        response_id = self._native_active_response_id
        if response_id:
            self._native_cancel_requested.add(response_id)
        return response_id

    async def cancel_native_response(self, response_id: str) -> None:
        if (
            not response_id
            or response_id in self._native_cancel_sent
            or self._conn is None
        ):
            return
        self._native_cancel_requested.add(response_id)
        self._native_cancel_sent.add(response_id)
        item_id = self._native_active_item_id
        started_at = self._native_audio_started_at
        generated_ms = int(self._native_audio_bytes / (REALTIME_PCM_SAMPLE_RATE * 2) * 1000)
        played_ms = (
            min(generated_ms, max(0, int((time.monotonic() - started_at) * 1000)))
            if started_at is not None
            else 0
        )
        if self._native_active_response_id == response_id:
            self._native_active_response_id = None
        self._native_active_item_id = None
        self._native_audio_started_at = None
        self._native_audio_bytes = 0
        try:
            await self._conn.send({"type": "response.cancel", "response_id": response_id})
            logger.info(
                "native_response_cancel_sent session_id=%s response_id=%s",
                self._session_id, response_id,
            )
            if item_id:
                await self._conn.send({
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": 0,
                    "audio_end_ms": played_ms,
                })
                logger.info(
                    "native_conversation_audio_truncated session_id=%s response_id=%s item_id=%s audio_end_ms=%d",
                    self._session_id, response_id, item_id, played_ms,
                )
        except Exception:
            logger.exception(
                "native_response_cancel_failed session_id=%s response_id=%s",
                self._session_id, response_id,
            )

    async def speak(self, *, client_turn_id: str, text: str, on_audio: AudioSink) -> SpeakResult:
        """Phase D: make Realtime speak the backend-APPROVED `text` in native
        voice, streaming the resulting 24kHz PCM to `on_audio` as it arrives.

        The response is created with conversation="none" so Realtime never
        builds its own dialogue state from what it speaks - the DB transcript
        stays authoritative. Realtime also returns its OWN transcript of the
        audio it produced; speak() returns it in SpeakResult.spoken_transcript
        so the caller can verify Realtime spoke the approved text verbatim
        (the critical Phase D fidelity check). Never raises - a
        connection/timeout problem returns a not-completed SpeakResult so the
        caller can fall back or mark the turn failed."""
        if not self.is_ready or self._conn is None or not text.strip():
            return SpeakResult("", 0, False, False)

        if not self._orphan_responses_drained.is_set():
            try:
                await asyncio.wait_for(
                    self._orphan_responses_drained.wait(), timeout=_RESPONSE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "realtime_orphan_response_drain_timeout session_id=%s client_turn_id=%s",
                    self._session_id, client_turn_id,
                )
            if not self.is_ready or self._conn is None:
                return SpeakResult("", 0, False, False)

        queue: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue()
        self._active_response = queue
        self._active_response_turn = client_turn_id
        self._active_response_id = None
        self._active_response_cancel_requested = False
        self._active_response_terminal = False
        self._active_response_cancelled_before_created = False
        spoken_parts: list[str] = []
        final_transcript: str | None = None
        audio_bytes = 0
        completed = False
        interrupted = False
        try:
            await self._conn.send({
                "type": "response.create",
                "response": {
                    "output_modalities": ["audio"],
                    "conversation": "none",
                    "instructions": _verbatim_instructions(text),
                },
            })
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=_RESPONSE_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.error(
                        "realtime_response_timeout session_id=%s client_turn_id=%s",
                        self._session_id, client_turn_id,
                    )
                    break
                if kind == "audio":
                    audio_bytes += len(payload)
                    await on_audio(payload)
                elif kind == "transcript_delta":
                    spoken_parts.append(payload)
                elif kind == "transcript_done":
                    final_transcript = payload
                elif kind == "cancelled":
                    interrupted = True
                    break
                elif kind == "error":
                    logger.error(
                        "realtime_response_error session_id=%s client_turn_id=%s error=%s",
                        self._session_id, client_turn_id, payload,
                    )
                    break
                elif kind == "done":
                    completed = True
                    break
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception:
            logger.exception(
                "realtime_response_speak_failed session_id=%s client_turn_id=%s",
                self._session_id, client_turn_id,
            )
        finally:
            self._active_response = None
            self._active_response_turn = None
            self._active_response_id = None
            self._active_response_cancel_requested = False
            self._active_response_terminal = False
            self._active_response_cancelled_before_created = False
        spoken = (final_transcript if final_transcript is not None else "".join(spoken_parts)).strip()
        logger.info(
            "realtime_response_spoken session_id=%s client_turn_id=%s audio_bytes=%d completed=%s "
            "interrupted=%s spoken_transcript=%r",
            self._session_id, client_turn_id, audio_bytes, completed, interrupted, spoken,
        )
        return SpeakResult(spoken, audio_bytes, completed, interrupted)

    async def cancel_active_response(self) -> None:
        """Interrupt the in-flight patient response (barge-in). Signals the
        waiting speak() loop to stop immediately, and asks OpenAI to cancel the
        response ONLY when one is genuinely active on the server.

        Two corrections from the first live run against gpt-realtime-2.1:
          - NEVER send output_audio_buffer.clear: it is not a valid Realtime
            WebSocket client event (the live API returns invalid_request_error/
            invalid_value). Immediate playback cutoff is handled by the worker
            clearing the LiveKit AudioSource queue, not here.
          - Only send response.cancel once we have seen response.created (i.e.
            self._active_response_id is set); sending it with no active response
            returns invalid_request_error/response_cancel_not_active.
        Idempotent: the response_id is consumed on the first cancel, so a
        duplicate interruption sends nothing further. Never raises."""
        queue = self._active_response
        if queue is None and self._native_active_response_id is not None:
            await self.cancel_native_response(self._native_active_response_id)
            return
        if (
            queue is None
            or self._active_response_cancel_requested
            or self._active_response_terminal
        ):
            return
        self._active_response_cancel_requested = True
        queue.put_nowait(("cancelled", None))
        conn = self._conn
        response_id = self._active_response_id
        if conn is None or response_id is None:
            # Nothing active on the server to cancel (or already cancelled) -
            # the ("cancelled") signal above still stops speak(); a late audio/
            # transcript event for this response is ignored by response_id
            # correlation in _route_response_event.
            if not self._active_response_cancelled_before_created:
                self._active_response_cancelled_before_created = True
                self._orphan_response_creates += 1
                self._orphan_responses_drained.clear()
            return
        # Consume the id and remember it as cancelled before awaiting the send.
        # The next speak() waits for its terminal response.done, while routing
        # drops any late audio/transcript carrying this id.
        self._active_response_id = None
        self._orphan_response_ids.add(response_id)
        self._orphan_responses_drained.clear()
        try:
            await conn.send({"type": "response.cancel", "response_id": response_id})
            logger.info(
                "realtime_response_cancel_sent session_id=%s response_id=%s",
                self._session_id, response_id,
            )
        except Exception:
            logger.exception(
                "realtime_response_cancel_send_failed session_id=%s response_id=%s",
                self._session_id, response_id,
            )
            # Local playback/speak() is already stopped. If the transport send
            # itself failed, do not block every later response for the full
            # response timeout; the retained id still rejects any late A PCM.
            self._orphan_responses_drained.set()

    async def _cancel_orphan_response(self, response_id: str) -> None:
        """Cancel a response whose response.created arrived after local cutoff."""
        conn = self._conn
        if conn is None:
            self._orphan_responses_drained.set()
            return
        try:
            await conn.send({"type": "response.cancel", "response_id": response_id})
            logger.info(
                "realtime_orphan_response_cancel_sent session_id=%s response_id=%s",
                self._session_id, response_id,
            )
        except Exception:
            logger.exception(
                "realtime_orphan_response_cancel_failed session_id=%s response_id=%s",
                self._session_id, response_id,
            )
            # Preserve the id for stale-event rejection but keep the session
            # usable when the cancellation request itself could not be sent.
            self._orphan_responses_drained.set()

    async def aclose(self) -> None:
        """Idempotent teardown: stop the loops, close the connection, log a
        one-line summary. Best-effort at every step so one failure never skips
        the others."""
        if self._close_requested:
            if self._run_task is not None and self._run_task is not asyncio.current_task():
                await asyncio.gather(self._run_task, return_exceptions=True)
            return
        self._close_requested = True
        self._closed = True
        self._close_reason = self._close_reason or "client_closed"
        self._configured_ready.clear()
        self._ready_or_terminated.set()
        self._orphan_responses_drained.set()
        conn = self._conn
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                logger.exception(
                    "realtime_session_conn_close_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "realtime_session_run_task_close_failed session_id=%s track=%s",
                    self._session_id, self._track_sid,
                )
        logger.info(
            "realtime_session_summary session_id=%s identity=%s track=%s frames_sent=%d "
            "frames_dropped=%d events=%s",
            self._session_id, self._identity, self._track_sid, self._frames_sent,
            self._frames_dropped, dict(self._event_counts),
        )


def _get(event: Any, key: str) -> Any:
    """Read a field from either a pydantic Realtime event object or a plain
    dict (tests use dicts) - keeps _handle_event provider-agnostic."""
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _verbatim_instructions(text: str) -> str:
    """Instruct Realtime to speak the backend-approved text WITHOUT altering
    the medical content. This is a best-effort constraint, not a guarantee -
    Realtime is a generative model, so speak()'s returned transcript is still
    compared against this text (the fidelity check) rather than trusted blindly
    (see the Phase D verbatim-fidelity requirement)."""
    return (
        "You are voicing a simulated patient. Read the following reply ALOUD "
        "exactly as written, word for word. Do not add, remove, reword, "
        "answer, explain, or comment - only speak these exact words:\n\n"
        f"{text}"
    )
