"""Phase B: turn the raw OpenAI Realtime event stream into exactly ONE
coherent, deduplicated backend student turn per completed utterance.

This is the POC's replacement for the legacy Smart Turn / _CandidateTurnCoordinator
HOLD/END machinery (worker.py): with semantic_vad, OpenAI itself decides WHEN
the student has finished - it does not emit input_audio_buffer.committed until
it judges the turn complete, so a mid-thought pause like

    "When your pain started..." [pause] "...were you walking or sitting?"

surfaces here as a SINGLE committed + a SINGLE transcription, i.e. one turn.
This controller's only job is to map that event pattern to one backend
submission with a stable, unique clientTurnId - it deliberately makes NO
turn-completion judgement of its own (that authority now lives entirely in
Realtime's semantic_vad; how well it holds a given pause is measured live in
Phase H, not asserted here).

Scope of THIS phase: detect completion, assign the clientTurnId, guarantee
exactly-once/no-phantom submission, and hand (clientTurnId, transcript) to a
callback. It does NOT call patient_engine, persist, or speak - Phase C wires
the callback body to the real patient pipeline. The controller holds no
reference to PocAgentSession state; a failure here is contained by the caller
(RealtimeSession), never reaching the legacy conversation path.
"""
from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger("app.livekit_agent.realtime")

# Realtime GA server-event types this controller reacts to (subset of the
# strings realtime_session.py logs - kept local so this module has no import
# coupling to the session's private constants).
_EVT_SPEECH_STARTED = "input_audio_buffer.speech_started"
_EVT_COMMITTED = "input_audio_buffer.committed"
_EVT_TRANSCRIPTION_DONE = "conversation.item.input_audio_transcription.completed"

# Bounds the dedup memory for submitted item ids across a long interview - a
# few dozen turns at most in practice, so this is a generous cap.
_MAX_SUBMITTED_ITEMS = 200

# Called synchronously once a deduplicated, non-empty transcript is accepted;
# only the returned awaitable is scheduled. This lets the owner reserve turn
# authority in event order before asynchronous processing begins.
TurnCompleteCallback = Callable[[str, str], Awaitable[None] | None]


class RealtimeTurnController:
    """One per interview (1:1 with a RealtimeSession). Not shared across
    sessions - the clientTurnId sequence and in-flight state are instance
    scoped, mirroring the isolation contract of _CandidateTurnCoordinator."""

    def __init__(
        self,
        *,
        session_id: str,
        on_turn_complete: TurnCompleteCallback,
    ) -> None:
        self._session_id = session_id
        self._on_turn_complete = on_turn_complete
        self._turn_seq = itertools.count(1)
        # The turn currently being assembled. `client_turn_id` is assigned on
        # the FIRST signal of a turn (speech_started, or committed/transcription
        # if speech_started was somehow missed) and stays stable across any
        # intra-turn VAD sub-segments until the turn completes and resets.
        self._active_client_turn_id: str | None = None
        self._active_item_id: str | None = None
        # Dedup: item ids already submitted (or deliberately skipped as empty),
        # so a duplicate/late transcription for the same item never produces a
        # second backend turn. Bounded, oldest-evicted.
        self._submitted_items: "OrderedDict[str, None]" = OrderedDict()

    def handle_event(self, event_type: str, event: Any) -> None:
        """Fed every server event by RealtimeSession. Only three types matter;
        everything else is ignored. Never raises."""
        try:
            if event_type == _EVT_SPEECH_STARTED:
                self._on_speech_started()
            elif event_type == _EVT_COMMITTED:
                self._on_committed(_get(event, "item_id"))
            elif event_type == _EVT_TRANSCRIPTION_DONE:
                self._on_transcription_done(_get(event, "item_id"), _get(event, "transcript"))
        except Exception:
            logger.exception(
                "realtime_turn_controller_event_failed session_id=%s event=%s",
                self._session_id, event_type,
            )

    def _ensure_turn(self) -> str:
        """Assign a fresh clientTurnId for a new turn, or return the active one.
        Namespaced 'realtime-...' so it can never collide with the browser's
        clientTurnId formats or the legacy 'semantic-...' ids."""
        if self._active_client_turn_id is None:
            self._active_client_turn_id = f"realtime-{self._session_id}-{next(self._turn_seq)}"
            logger.info(
                "realtime_turn_started session_id=%s client_turn_id=%s",
                self._session_id, self._active_client_turn_id,
            )
        return self._active_client_turn_id

    def _on_speech_started(self) -> None:
        # Starts a turn only if none is active - repeated speech_started within
        # the same semantic turn (VAD sub-segments across a held pause) keep the
        # SAME clientTurnId. semantic_vad decides the boundary, not us.
        self._ensure_turn()

    def _on_committed(self, item_id: str | None) -> None:
        # Realtime committed the user audio buffer as a conversation item; the
        # authoritative transcript for it arrives shortly after, keyed by the
        # same item_id.
        self._ensure_turn()
        self._active_item_id = item_id

    def _on_transcription_done(self, item_id: str | None, transcript: Any) -> None:
        if item_id is not None and item_id in self._submitted_items:
            logger.info(
                "realtime_turn_duplicate_transcription_ignored session_id=%s item_id=%s",
                self._session_id, item_id,
            )
            return
        client_turn_id = self._ensure_turn()
        text = (transcript or "").strip() if isinstance(transcript, str) else ""

        # Mark the item resolved BEFORE anything else so a duplicate/late
        # transcription for it can never re-fire, then reset for the next turn.
        if item_id is not None:
            self._mark_item_submitted(item_id)
        self._active_client_turn_id = None
        self._active_item_id = None

        if not text:
            # Never submit an empty turn (no phantom patient response).
            logger.info(
                "realtime_turn_empty_transcript_skipped session_id=%s client_turn_id=%s item_id=%s",
                self._session_id, client_turn_id, item_id or "-",
            )
            return

        logger.info(
            "realtime_turn_complete session_id=%s client_turn_id=%s item_id=%s transcript=%r",
            self._session_id, client_turn_id, item_id or "-", text,
        )
        # Fire-and-forget (Phase C's patient pipeline can take seconds) - the
        # receive loop must keep draining Realtime events meanwhile.
        processing = self._on_turn_complete(client_turn_id, text)
        if processing is not None:
            asyncio.ensure_future(processing)

    def _mark_item_submitted(self, item_id: str) -> None:
        self._submitted_items[item_id] = None
        self._submitted_items.move_to_end(item_id)
        while len(self._submitted_items) > _MAX_SUBMITTED_ITEMS:
            self._submitted_items.popitem(last=False)


def _get(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)
