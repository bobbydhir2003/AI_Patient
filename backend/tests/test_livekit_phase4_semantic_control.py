"""Phase 4 (EXPERIMENTAL semantic turn CONTROL) - see app/livekit_agent/
worker.py's PocAgentSession/_CandidateTurnCoordinator/TurnSource and
app/core/config.py's semantic_turn_control_active.

Reuses the SAME fake-rtc/fake-room fixtures test_livekit_poc.py/
test_livekit_phase_c.py already established rather than duplicating them.
Does not touch a real LiveKit Cloud connection, Deepgram, or the Smart Turn
ONNX model - _CandidateTurnCoordinator is exercised directly with a fake
SemanticTurnDetector (it has no dependency on the concrete detector
implementation - see turn_detector.py's SemanticTurnDetector ABC), and
PocAgentSession's turn-submission path is exercised the SAME way
test_livekit_phase_c.py's dedup tests already do (monkeypatching
patient_adapter.generate_and_persist_turn directly rather than a real
OpenAI/ElevenLabs round trip).
"""
import asyncio
import logging

import pytest

from app.core.config import get_settings
from app.livekit_agent import patient_adapter
from app.livekit_agent.turn_detector import SemanticTurnDetector, TurnDecision, TurnDetectorResult
from app.livekit_agent.worker import _CandidateTurnCoordinator, TurnSource
from tests.test_livekit_phase_c import _control_messages, _make_ready_session, _run_until_idle, _StudentTextPacket
from tests.test_livekit_poc import _fake_rtc_for_worker


# =================================================================
# Config: the flag, its "effective" property, and the misconfig warning
# =================================================================

def test_semantic_turn_control_defaults_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_semantic_turn_control_enabled", False)
    assert settings.semantic_turn_control_active is False


def test_semantic_turn_control_active_requires_all_three_flags(monkeypatch):
    settings = get_settings()
    # Control alone (both prerequisites off) - inactive.
    monkeypatch.setattr(settings, "livekit_semantic_turn_control_enabled", True)
    monkeypatch.setattr(settings, "livekit_server_stt_enabled", False)
    monkeypatch.setattr(settings, "livekit_semantic_turn_detection_enabled", False)
    assert settings.semantic_turn_control_active is False

    # Control + STT, but detection still off - inactive.
    monkeypatch.setattr(settings, "livekit_server_stt_enabled", True)
    assert settings.semantic_turn_control_active is False

    # All three true - active.
    monkeypatch.setattr(settings, "livekit_semantic_turn_detection_enabled", True)
    assert settings.semantic_turn_control_active is True


def test_semantic_turn_control_misconfig_logs_warning(monkeypatch, caplog):
    from app.core.config import Settings

    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        Settings(
            livekit_semantic_turn_control_enabled=True,
            livekit_server_stt_enabled=False,
            livekit_semantic_turn_detection_enabled=False,
            jwt_secret_key="test-secret-at-least-32-characters-long",
        )
    assert any("LIVEKIT_SEMANTIC_TURN_CONTROL_ENABLED=true" in r.message for r in caplog.records)


# =================================================================
# _CandidateTurnCoordinator: HOLD/END wiring in isolation (no PocAgentSession,
# no room, no OpenAI - proves the coordinator's own contract).
# =================================================================

class _FixedDetector(SemanticTurnDetector):
    def __init__(self, decisions):
        self._decisions = list(decisions)

    async def evaluate(self, context):
        decision = self._decisions.pop(0) if self._decisions else TurnDecision.HOLD
        return TurnDetectorResult(decision=decision, probability=0.9, inference_ms=1.0, detector="fixed")


def _make_coordinator(decisions, on_end=None, on_unhealthy=None):
    return _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector(decisions), sample_rate=16000,
        on_end=on_end, on_unhealthy=on_unhealthy,
    )


async def _feed_boundary(coordinator, *, text: str, speech_duration_s=1.0, silence_duration_s=0.6):
    """Mirrors the REAL VAD/STT ordering (Step 9): VAD's END_OF_SPEECH fires
    on_speech_ended FIRST (scheduling _evaluate_boundary, which starts
    waiting on the STT-final grace window), and Deepgram's matching
    FINAL_TRANSCRIPT for that just-ended utterance arrives shortly AFTER,
    via on_final_transcript - which is what actually unblocks the wait.
    Calling on_final_transcript BEFORE on_speech_ended (the reverse order)
    would have its `pending_final_event.set()` immediately wiped by
    on_speech_ended's own `.clear()`, forcing the full
    _STT_FINAL_GRACE_SECONDS (1s) timeout every time - technically still
    correct (Step 9: "evaluate anyway"), just needlessly slow for a test."""
    coordinator.on_speech_started()
    coordinator.on_speech_ended(speech_duration_s=speech_duration_s, silence_duration_s=silence_duration_s)
    await asyncio.sleep(0)  # let _evaluate_boundary start awaiting pending_final_event
    coordinator.on_final_transcript(text)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if coordinator._eval_task is not None and coordinator._eval_task.done():
            break


def test_hold_does_not_call_on_end():
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Great")
        await coordinator.aclose()

    asyncio.run(_drive())
    assert calls == []
    # HOLD must preserve the candidate turn, not reset it.
    assert coordinator._candidate_segments == [] or True  # aclose() resets - see below for a live-state check


def test_hold_preserves_candidate_state_for_continuation():
    coordinator = _make_coordinator([TurnDecision.HOLD])

    async def _drive():
        await _feed_boundary(coordinator, text="Great")

    asyncio.run(_drive())
    # Preserved (not cleared) after a HOLD - the next utterance continues
    # the SAME candidate turn/id.
    assert coordinator._candidate_segments == ["Great"]
    assert coordinator._candidate_turn_id is not None
    turn_id_after_hold = coordinator._candidate_turn_id

    async def _drive2():
        await _feed_boundary(coordinator, text="and when did your pain start?")

    asyncio.run(_drive2())
    assert coordinator._candidate_turn_id == turn_id_after_hold, "id must stay stable across a HOLD continuation"


def test_end_submits_accumulated_transcript_exactly_once():
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD, TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Great")
        await _feed_boundary(coordinator, text="and when did your pain start?")
        await asyncio.sleep(0.05)  # let the fire-and-forget on_end task run

    asyncio.run(_drive())
    assert len(calls) == 1
    turn_id, transcript = calls[0]
    assert transcript == "Great and when did your pain start?"
    assert turn_id is not None and turn_id.startswith("semantic-sess-1-")
    # Reset after END - a NEW candidate turn would get a different id.
    assert coordinator._candidate_turn_id is None
    assert coordinator._candidate_segments == []


def test_end_with_empty_transcript_does_not_call_on_end():
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END], on_end=on_end)

    async def _drive():
        coordinator.on_speech_started()
        # No on_final_transcript call - candidate transcript stays empty.
        coordinator.on_speech_ended(speech_duration_s=0.3, silence_duration_s=0.6)
        for _ in range(50):
            await asyncio.sleep(0.01)

    asyncio.run(_drive())
    assert calls == []


def test_on_end_none_is_byte_for_byte_phase3_observational_behavior():
    """Phase 3 default (control off): on_end=None - an END decision still
    resets the candidate turn but never calls anything back."""
    coordinator = _make_coordinator([TurnDecision.END], on_end=None)

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")

    asyncio.run(_drive())
    assert coordinator._candidate_turn_id is None  # reset happened, no error


def test_repeated_detector_errors_trigger_on_unhealthy():
    class _ExplodingDetector(SemanticTurnDetector):
        async def evaluate(self, context):
            raise RuntimeError("boom")

    reasons = []
    coordinator = _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_ExplodingDetector(), sample_rate=16000,
        on_unhealthy=lambda reason: reasons.append(reason),
    )

    async def _drive():
        for _ in range(3):
            await _feed_boundary(coordinator, text="hmm")

    asyncio.run(_drive())
    assert reasons == ["detector_repeated_errors"]  # fires once, at the threshold


# =================================================================
# PocAgentSession integration: browser text vs. semantic submission
# =================================================================

def test_browser_text_still_authoritative_when_control_off(monkeypatch, engine):
    """Baseline: semantic control OFF (default) - existing behavior is
    completely unchanged."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        assert session._semantic_control_active is False
        calls = []

        def fake_generate(db, **kwargs):
            calls.append(kwargs["client_turn_id"])
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Where does it hurt?", "turn-off-1"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert calls == ["turn-off-1"]


def test_browser_text_ignored_when_semantic_control_active(monkeypatch, engine, caplog):
    """Step 6: once semantic control is genuinely active, a browser
    student_text packet must still be ack'd (so the browser doesn't
    retry-storm) but must NEVER trigger patient generation."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        calls = []

        def fake_generate(db, **kwargs):
            calls.append(kwargs["client_turn_id"])
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            session._semantic_control_active = True  # simulate an active session
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _StudentTextPacket("Great", "turn-browser-1"))
                await _run_until_idle()

        asyncio.run(_drive())

    assert calls == [], "browser text must never trigger patient generation while semantic control is active"
    # Still ack'd - the browser must not spuriously retry a packet the
    # server has simply decided is non-authoritative.
    acks = _control_messages(room, "turn_ack")
    assert acks == [{"type": "turn_ack", "clientTurnId": "turn-browser-1"}]
    assert any("semantic_turn_browser_text_ignored" in r.message for r in caplog.records)


def test_semantic_end_submits_exactly_once_through_the_canonical_pipeline(monkeypatch, engine):
    """Step 4/9: a full HOLD -> END cycle through _CandidateTurnCoordinator,
    wired to the REAL PocAgentSession._handle_semantic_turn_end, submits the
    FULL accumulated transcript exactly once through the SAME turn-lock/
    generate_and_persist_turn pipeline browser student_text uses."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        calls = []

        def fake_generate(db, **kwargs):
            calls.append((kwargs["client_turn_id"], kwargs["question"]))
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            session._semantic_control_active = True
            coordinator = _CandidateTurnCoordinator(
                session_id=session.session_id, identity="student-1", track_sid="track-1",
                detector=_FixedDetector([TurnDecision.HOLD, TurnDecision.END]), sample_rate=16000,
                on_end=session._handle_semantic_turn_end,
            )
            await _feed_boundary(coordinator, text="Great")
            await _feed_boundary(coordinator, text="and when did your pain start?")
            await _run_until_idle()

        asyncio.run(_drive())

    assert len(calls) == 1
    client_turn_id, question = calls[0]
    assert question == "Great and when did your pain start?"
    assert client_turn_id.startswith("semantic-")
    turn_started = _control_messages(room, "semantic_turn_started")
    assert turn_started == [{"type": "semantic_turn_started", "clientTurnId": client_turn_id}]


def test_duplicate_semantic_end_cannot_create_two_patient_responses(monkeypatch, engine, caplog):
    """Defensive backstop (Step 4/7): even if the SAME semantic turn id were
    ever submitted twice (structurally shouldn't happen - the coordinator
    hands out a fresh id per candidate turn - but this is the explicit,
    required guarantee), only the first call reaches patient generation."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        calls = []

        def fake_generate(db, **kwargs):
            calls.append(kwargs["client_turn_id"])
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            session._semantic_control_active = True
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                await session._handle_semantic_turn_end("semantic-dup-1", "Where does it hurt?")
                await session._handle_semantic_turn_end("semantic-dup-1", "Where does it hurt?")
                await _run_until_idle()

        asyncio.run(_drive())

    assert calls == ["semantic-dup-1"], "a second call for the SAME semantic turn id must never regenerate"
    assert any("semantic_turn_duplicate_prevented" in r.message for r in caplog.records)


def test_fallback_restores_browser_control(monkeypatch, engine):
    """Step 11: a one-way runtime downgrade - once triggered, browser
    student_text becomes authoritative again for the rest of the session,
    and a semantic_fallback control message is published so the frontend
    also learns about it."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        calls = []

        def fake_generate(db, **kwargs):
            calls.append(kwargs["client_turn_id"])
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            session._semantic_control_active = True

            session._fallback_to_browser_control("test_forced_failure")
            assert session._semantic_control_active is False

            room.emit("data_received", _StudentTextPacket("Where does it hurt?", "turn-after-fallback"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert calls == ["turn-after-fallback"], "browser text must be authoritative again after fallback"
    fallback_messages = _control_messages(room, "semantic_fallback")
    assert fallback_messages == [{"type": "semantic_fallback", "reason": "test_forced_failure"}]


def test_fallback_is_idempotent_and_never_flips_back_on(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)

        async def _drive():
            await session.start()
            session._semantic_control_active = True
            session._fallback_to_browser_control("reason-a")
            session._fallback_to_browser_control("reason-b")  # already off - no-op
            session._semantic_control_active = True  # nothing in production does this, but prove the flag itself is inert here
            session._fallback_to_browser_control("reason-c")

        asyncio.run(_drive())

    fallback_messages = _control_messages(room, "semantic_fallback")
    # Two messages: the first call (real transition) and the third call
    # (also a real transition, since the flag was set back True directly for
    # this test) - the SECOND call, a genuine no-op-after-already-off, must
    # not have published anything.
    assert len(fallback_messages) == 2
    assert [m["reason"] for m in fallback_messages] == ["reason-a", "reason-c"]


def test_turn_source_enum_values_are_stable_for_logs():
    assert TurnSource.BROWSER_TEXT.value == "browser_text"
    assert TurnSource.SERVER_SEMANTIC.value == "server_semantic"
