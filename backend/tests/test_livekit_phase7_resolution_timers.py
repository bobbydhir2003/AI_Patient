"""Phase 7 (EXPERIMENTAL semantic resolution timers) - see app/livekit_agent/
worker.py's _CandidateTurnCoordinator._arm_pending_resolution/
_cancel_pending_resolution/_resolve_pending_resolution/
_commit_pending_resolution and app/core/config.py's
semantic_resolution_timers_active.

Fixes two confirmed turn-taking bugs on top of Phase 4 turn CONTROL:

1. False HOLD recovery: a Smart Turn HOLD decision previously had NO
   recovery mechanism at all. If Smart Turn misjudged a genuinely complete
   question ("What brings you in today?") as HOLD, the candidate turn
   stayed open for the rest of the session - at most one backchannel
   ("Mm-hmm.") played, then permanent silence, never an OpenAI call.

2. Universal END grace: a Smart Turn END decision previously reset and
   submitted the candidate turn immediately, so a brief natural
   mid-question pause ("When your pain started... were you walking or
   sitting?") could split into two unrelated turns.

Both are a SINGLE pending-resolution slot per coordinator (never two
independent timer states that could coexist), cancelled the instant
on_speech_started fires (same semantic turn continues, nothing submitted),
and converging on ONE shared commit path that reuses the exact
turn-lock/generate_and_persist_turn pipeline browser student_text and the
pre-Phase-7 immediate-END path already used.

Reuses the SAME fake-detector/fake-rtc/fake-room fixtures the Phase 4/5A/6
test files already established. Timer durations are shrunk via
monkeypatch.setattr on the module constants - the SAME technique
test_livekit_phase6_backchannel.py already uses for
_BACKCHANNEL_HOLD_DELAY_SECONDS - so these tests stay fast without
changing any actual timing logic under test.
"""
import asyncio
import logging
import time

from app.livekit_agent import patient_adapter
from app.livekit_agent.turn_detector import SemanticTurnDetector, TurnDecision, TurnDetectorResult
from app.livekit_agent.worker import _CandidateTurnCoordinator, _StudentVadSttPipeline
from tests.test_livekit_phase4_semantic_control import _FixedDetector, _feed_boundary
from tests.test_livekit_phase_c import _control_messages, _make_ready_session, _run_until_idle
from tests.test_livekit_phase6_backchannel import _make_backchannel_session, _start_with_backchannel
from tests.test_livekit_poc import _fake_rtc_for_worker

_COMPLETE_QUESTION = "What brings you in today?"
_LONG_ENOUGH = "So when you first noticed the pain"  # clears _MIN_BACKCHANNEL_WORDS


def _make_coordinator(
    decisions, *, on_end=None, on_hold=None, on_before_commit=None, resolution_timers_enabled=True,
):
    return _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector(decisions), sample_rate=16000,
        on_end=on_end, on_hold=on_hold, on_before_commit=on_before_commit,
        resolution_timers_enabled=resolution_timers_enabled,
    )


async def _wait_until(predicate, *, iterations=50, interval=0.02):
    for _ in range(iterations):
        if predicate():
            return
        await asyncio.sleep(interval)


# =================================================================
# Coordinator-level: HOLD recovery arm/cancel/commit (scenarios A, C, D).
# =================================================================

def test_hold_recovery_commits_exactly_once_after_silence(monkeypatch):
    """Scenario A: a complete question false-HOLDed by Smart Turn must
    eventually submit exactly once even if the student never speaks again -
    this is the exact production symptom (patient says only "Mm-hmm." then
    nothing) this phase fixes."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.05)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        assert coordinator._pending_kind == "hold_recovery"  # armed immediately after HOLD
        await _wait_until(lambda: len(calls) == 1)

    asyncio.run(_drive())
    assert len(calls) == 1
    turn_id, transcript = calls[0]
    assert transcript == _COMPLETE_QUESTION
    assert turn_id.startswith("semantic-sess-1-")
    assert coordinator._candidate_turn_id is None, "reset after commit"
    assert coordinator._pending_kind is None, "pending-resolution slot cleared after commit"


def test_hold_recovery_cancelled_by_resume_same_turn_continues(monkeypatch):
    """Scenario C: the student resuming before the deadline must cancel
    recovery, preserve the SAME semantic turn id, and never submit."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.3)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD, TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Can you please tell me")
        turn_id_after_hold = coordinator._candidate_turn_id
        assert coordinator._pending_kind == "hold_recovery"
        await asyncio.sleep(0.05)  # well before the 0.3s deadline
        await _feed_boundary(coordinator, text="where the pain is?")
        assert coordinator._candidate_turn_id == turn_id_after_hold, "same turn id preserved"
        await asyncio.sleep(0.4)  # past the ORIGINAL (cancelled) deadline

    asyncio.run(_drive())
    assert len(calls) == 1, "exactly one submission, from the real END, not the cancelled recovery"
    assert calls[0][1] == "Can you please tell me where the pain is?"


def test_hold_recovery_resume_just_before_deadline_wins(monkeypatch):
    """Scenario D: resuming with only a small margin before the deadline
    must still safely cancel it - no premature submission."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.15)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)
        await asyncio.sleep(0.1)  # 50ms of margin remains before the 0.15s deadline
        coordinator.on_speech_started()  # student resumes
        await asyncio.sleep(0.3)  # well past the original deadline

    asyncio.run(_drive())
    assert calls == [], "resuming before the deadline must never let recovery commit"
    assert coordinator._pending_kind is None
    assert coordinator._candidate_turn_id is not None, "turn stays open, not reset"


def test_hold_recovery_deadline_anchored_to_vad_end_not_decision_time(monkeypatch):
    """The deadline must be computed from _vad_end_at (the VAD END_OF_SPEECH
    boundary), not from time.monotonic() at HOLD-decision time - otherwise
    STT-grace-wait/inference latency would stack on top of the budget
    instead of counting against it (see the Phase 7 design doc's timing
    model)."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.2)

    class _SlowDetector(SemanticTurnDetector):
        async def evaluate(self, context):
            await asyncio.sleep(0.1)  # simulates STT-grace-wait/inference latency
            return TurnDetectorResult(decision=TurnDecision.HOLD, probability=0.4, inference_ms=100.0, detector="slow")

    async def on_end(turn_id, transcript):
        pass

    coordinator = _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_SlowDetector(), sample_rate=16000,
        on_end=on_end, resolution_timers_enabled=True,
    )
    result = {}

    async def _drive():
        # _feed_boundary sends the matching final transcript right after
        # on_speech_ended (as Deepgram normally would), so the STT-grace
        # wait resolves immediately - the only latency in this test is the
        # _SlowDetector's own 0.1s "inference" delay, standing in for
        # whatever real STT-grace-wait/inference latency the production
        # system spends between VAD END and the HOLD decision.
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        assert coordinator._pending_kind == "hold_recovery"
        # HOLD never clears _vad_end_at (only a reset does), so it still
        # holds the timestamp captured at on_speech_ended - the moment
        # BEFORE the detector's own 0.1s delay was spent.
        result["time_spent_before_arming"] = coordinator._pending_armed_at - coordinator._vad_end_at

        # Observable consequence: the REMAINING wait from arm-time to
        # commit must be roughly (0.2s budget - 0.1s already spent) =
        # ~0.1s, not the full 0.2s a naive `now + 0.2s` anchor would
        # produce - this is the actual, user-facing effect of anchoring to
        # vad_end_at rather than decision time.
        arm_time = time.monotonic()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if coordinator._pending_kind is None:
                result["remaining_wait"] = time.monotonic() - arm_time
                return
        result["remaining_wait"] = None

    asyncio.run(_drive())
    assert result["time_spent_before_arming"] >= 0.09, (
        "the detector's own 0.1s delay must show up as elapsed time between "
        "vad_end_at and when the timer was actually armed"
    )
    assert result["remaining_wait"] is not None, "recovery must have committed"
    assert result["remaining_wait"] < 0.19, (
        f"remaining wait was {result['remaining_wait']:.3f}s - should be close to 0.1s "
        "(0.2s budget minus the 0.1s the detector already spent), not the "
        "full 0.2s a decision-time anchor would produce"
    )


# =================================================================
# Coordinator-level: PENDING_END arm/cancel/commit (scenarios F, G).
# =================================================================

def test_pending_end_commits_exactly_once_after_grace(monkeypatch):
    """Scenario G: a true END with no continuation must submit exactly once
    after the universal grace window."""
    monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 0.05)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")
        assert coordinator._pending_kind == "pending_end"  # not reset immediately
        assert coordinator._candidate_turn_id is not None
        await _wait_until(lambda: len(calls) == 1)

    asyncio.run(_drive())
    assert len(calls) == 1
    assert calls[0][1] == "Where does it hurt?"
    assert coordinator._candidate_turn_id is None


def test_pending_end_cancelled_by_resume_same_turn(monkeypatch):
    """Scenario F: 'When your pain started...' false-ENDed, student resumes
    within the grace window with '...were you walking or sitting?' - must
    merge into ONE semantic turn, not split into two."""
    monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 0.4)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END, TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="When your pain started")
        turn_id_after_end = coordinator._candidate_turn_id
        assert coordinator._pending_kind == "pending_end"
        await asyncio.sleep(0.1)  # well within the 0.4s grace
        await _feed_boundary(coordinator, text="were you walking or sitting?")
        assert coordinator._candidate_turn_id == turn_id_after_end, "same turn id - never split"
        await _wait_until(lambda: len(calls) == 1)

    asyncio.run(_drive())
    assert len(calls) == 1, "exactly one submission for the combined utterance"
    assert calls[0][1] == "When your pain started were you walking or sitting?"


def test_hold_then_end_merges_transcript_unchanged_with_flag_on(monkeypatch):
    """Scenario E (regression): the ALREADY-WORKING HOLD -> continuation ->
    END merge (Production Example 2 from the original audit) must remain
    unchanged with resolution timers enabled."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)
    monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 0.03)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD, TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Can you please tell me")
        await _feed_boundary(coordinator, text="you're walking or sitting?")
        await _wait_until(lambda: len(calls) == 1)

    asyncio.run(_drive())
    assert len(calls) == 1
    assert calls[0][1] == "Can you please tell me you're walking or sitting?"


def test_flag_off_end_is_byte_for_byte_immediate_submit():
    """Regression: with the flag off (default), END must submit
    synchronously/immediately - no pending-resolution slot ever armed."""
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END], on_end=on_end, resolution_timers_enabled=False)

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")
        await asyncio.sleep(0.05)  # let the fire-and-forget on_end task run

    asyncio.run(_drive())
    assert len(calls) == 1
    assert coordinator._pending_kind is None, "flag off - pending-resolution machinery never engages"
    assert coordinator._candidate_turn_id is None, "reset happened immediately, as before this phase"


def test_flag_off_hold_never_arms_recovery():
    """Regression: with the flag off (default), HOLD must never arm a
    recovery timer - byte-for-byte pre-Phase-7 behavior."""
    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=lambda *a: None, resolution_timers_enabled=False)

    async def _drive():
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        await asyncio.sleep(0.05)

    asyncio.run(_drive())
    assert coordinator._pending_kind is None
    assert coordinator._candidate_turn_id is not None, "HOLD still preserves the candidate, unchanged"


# =================================================================
# Coordinator-level: single-slot invariant, races, teardown (scenarios
# H, I, J, K).
# =================================================================

def test_single_pending_resolution_slot_invariant(monkeypatch):
    """'One semantic candidate -> at most one pending resolution' - arming
    a new resolution must never leave a stale one coexisting."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)
    coordinator = _make_coordinator([TurnDecision.HOLD, TurnDecision.HOLD], on_end=lambda *a: None)

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)
        first_task = coordinator._pending_task
        assert coordinator._pending_kind == "hold_recovery"
        await _feed_boundary(coordinator, text="and it hurts more at night")
        # A second HOLD re-arms - must be a FRESH task, never two coexisting.
        assert coordinator._pending_kind == "hold_recovery"
        assert coordinator._pending_task is not first_task
        assert first_task.cancelled() or first_task.done()

    asyncio.run(_drive())


def test_pending_end_timer_and_speech_start_race_never_double_submits(monkeypatch):
    """Scenario H: force the deadline to fire essentially immediately and
    race on_speech_started against it - whichever order the event loop
    picks, there must never be two submissions and never corrupted state."""
    monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 0.0)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")
        # The pending task is already scheduled with delay=0 - immediately
        # racing on_speech_started against it exercises both possible
        # orderings across repeated runs without flaking either way.
        coordinator.on_speech_started()
        await _run_until_idle()

    asyncio.run(_drive())
    assert len(calls) <= 1, "never more than one submission under this race"
    if calls:
        # If the commit won the race, the turn was correctly retired and a
        # fresh one started for the "resumed" speech.
        assert coordinator._candidate_turn_id is not None
        assert coordinator._candidate_turn_id != calls[0][0]
    else:
        # If the cancellation won, the SAME turn must still be alive.
        assert coordinator._candidate_turn_id is not None


def test_hold_recovery_timer_and_speech_start_race_never_double_submits(monkeypatch):
    """Scenario I: same race, HOLD_RECOVERY side."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.0)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        coordinator.on_speech_started()
        await _run_until_idle()

    asyncio.run(_drive())
    assert len(calls) <= 1, "never more than one submission under this race"


def test_aclose_during_hold_recovery_cancels_cleanly(monkeypatch):
    """Scenario J (coordinator half): shutdown while HOLD recovery is armed
    must cancel it cleanly - on_end must never fire."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        assert coordinator._pending_kind == "hold_recovery"
        await coordinator.aclose()

    asyncio.run(_drive())
    assert calls == []
    assert coordinator._pending_kind is None


def test_aclose_during_pending_end_cancels_cleanly(monkeypatch):
    """Scenario J: same, PENDING_END side."""
    monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 5.0)
    calls = []

    async def on_end(turn_id, transcript):
        calls.append((turn_id, transcript))

    coordinator = _make_coordinator([TurnDecision.END], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")
        assert coordinator._pending_kind == "pending_end"
        await coordinator.aclose()

    asyncio.run(_drive())
    assert calls == []
    assert coordinator._pending_kind is None


def test_two_coordinators_do_not_share_pending_state(monkeypatch):
    """Scenario K: multiple sessions' timer state must stay isolated."""
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)
    coordinator_a = _CandidateTurnCoordinator(
        session_id="sess-a", identity="student-a", track_sid="track-a",
        detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
        on_end=lambda *a: None, resolution_timers_enabled=True,
    )
    coordinator_b = _CandidateTurnCoordinator(
        session_id="sess-b", identity="student-b", track_sid="track-b",
        detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
        on_end=lambda *a: None, resolution_timers_enabled=True,
    )

    async def _drive():
        await _feed_boundary(coordinator_a, text=_COMPLETE_QUESTION)

    asyncio.run(_drive())
    assert coordinator_a._pending_kind == "hold_recovery"
    assert coordinator_b._pending_kind is None, "coordinator B must be completely unaffected"
    assert coordinator_b._candidate_turn_id is None


def test_diagnostic_pending_resolution_state_is_read_only(monkeypatch):
    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)
    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=lambda *a: None)

    async def _drive():
        kind, turn_id, elapsed_ms = coordinator.diagnostic_pending_resolution_state()
        assert (kind, turn_id, elapsed_ms) == (None, None, None)
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        kind, turn_id, elapsed_ms = coordinator.diagnostic_pending_resolution_state()
        assert kind == "hold_recovery"
        assert turn_id == coordinator._candidate_turn_id
        assert elapsed_ms is not None and elapsed_ms >= 0
        # Read-only - calling it again must not mutate anything.
        assert coordinator.diagnostic_pending_resolution_state()[0] == "hold_recovery"

    asyncio.run(_drive())


def test_deepgram_native_end_of_speech_is_diagnostic_only(monkeypatch, caplog):
    """Scenario L: the plugin's own derived END_OF_SPEECH (from Deepgram's
    speech_final) must only be logged - never submit, alter, or shorten
    anything. _consume_stt_events has no code path from this branch into
    any coordinator-mutating call."""
    from livekit.agents import stt as agents_stt

    monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 5.0)

    class _FakeStream:
        def __init__(self, events):
            self._events = events

        def push_frame(self, frame):
            pass

        async def aclose(self):
            pass

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for event in self._events:
                yield event

    class _FakeVAD:
        def stream(self):
            return _FakeStream([])

    class _FakeSTT:
        def stream(self):
            return _FakeStream([agents_stt.SpeechEvent(type=agents_stt.SpeechEventType.END_OF_SPEECH)])

    async def on_end(turn_id, transcript):
        pass

    coordinator = _make_coordinator([TurnDecision.HOLD], on_end=on_end)

    async def _drive():
        await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
        assert coordinator._pending_kind == "hold_recovery"
        turn_id_before = coordinator._candidate_turn_id
        with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
            pipeline = _StudentVadSttPipeline(
                session_id="sess-1", identity="student-1", track_sid="track-1",
                vad=_FakeVAD(), stt=_FakeSTT(), candidate_turn=coordinator,
            )
            await asyncio.sleep(0.1)
            # Assert BEFORE aclose() (which legitimately resets everything
            # on its own, unrelated to the diagnostic event) - the
            # END_OF_SPEECH event itself must not have changed anything.
            assert coordinator._candidate_turn_id == turn_id_before, "must not alter the candidate turn"
            assert coordinator._pending_kind == "hold_recovery", "must not cancel/shorten the armed timer"
            await pipeline.aclose()
        assert coordinator._pending_kind is None, "aclose() cancels it - a normal teardown, not a decision change"

    asyncio.run(_drive())
    assert any("student_stt_native_end_of_speech" in r.message for r in caplog.records)
    assert any("pending_resolution_kind=hold_recovery" in r.message for r in caplog.records)


# =================================================================
# PocAgentSession-level: real end-to-end pipeline (scenarios A, B, F).
# =================================================================

def test_false_hold_recovery_reaches_real_patient_pipeline(monkeypatch, engine):
    """Scenario A end-to-end: a false HOLD on a complete question, wired to
    the REAL PocAgentSession._handle_semantic_turn_end, must eventually
    reach the SAME canonical turn-lock/generate_and_persist_turn pipeline
    browser student_text uses - never permanent silence."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.05)
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
                detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
                on_end=session._handle_semantic_turn_end, resolution_timers_enabled=True,
            )
            await _feed_boundary(coordinator, text=_COMPLETE_QUESTION)
            await _wait_until(lambda: len(calls) == 1)

        asyncio.run(_drive())

    assert len(calls) == 1, "the student must never be left with permanent silence"
    client_turn_id, question = calls[0]
    assert question == _COMPLETE_QUESTION
    turn_started = _control_messages(room, "semantic_turn_started")
    assert turn_started == [{"type": "semantic_turn_started", "clientTurnId": client_turn_id}]


def test_false_hold_recovery_with_backchannel_still_reaches_pipeline(monkeypatch, engine):
    """Scenario B end-to-end: backchannel ('Mm-hmm.') may play during the
    recovery window, but recovery must still commit afterward - the
    backchannel must never be mistaken for, or block, the real answer."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)
        monkeypatch.setattr("app.livekit_agent.worker._HOLD_RECOVERY_SECONDS", 0.2)
        calls = []

        def fake_generate(db, **kwargs):
            calls.append((kwargs["client_turn_id"], kwargs["question"]))
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await _start_with_backchannel(session)
            session._resolution_timers_enabled = True
            coordinator = _CandidateTurnCoordinator(
                session_id=session.session_id, identity="student-1", track_sid="track-1",
                detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
                on_end=session._handle_semantic_turn_end,
                on_hold=session._on_semantic_hold,
                on_student_resumed=session._cancel_pending_backchannel,
                on_before_commit=session._cancel_pending_backchannel,
                resolution_timers_enabled=True,
            )
            await _feed_boundary(coordinator, text=_LONG_ENOUGH)
            await _wait_until(lambda: len(calls) == 1, iterations=100)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is not None, "backchannel should have played once"
    assert len(calls) == 1, "recovery must still commit after the backchannel - never stuck on Mm-hmm."
    assert calls[0][1] == _LONG_ENOUGH


def test_pending_end_resume_within_grace_end_to_end(monkeypatch, engine):
    """Scenario F end-to-end: a brief natural pause must not split one
    utterance into two turns through the real submission pipeline."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr("app.livekit_agent.worker._PENDING_END_GRACE_SECONDS", 0.4)
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
                detector=_FixedDetector([TurnDecision.END, TurnDecision.END]), sample_rate=16000,
                on_end=session._handle_semantic_turn_end, resolution_timers_enabled=True,
            )
            await _feed_boundary(coordinator, text="When your pain started")
            await asyncio.sleep(0.1)  # within the 0.4s grace
            await _feed_boundary(coordinator, text="were you walking or sitting?")
            await _wait_until(lambda: len(calls) == 1, iterations=100)

        asyncio.run(_drive())

    assert len(calls) == 1, "exactly one submission - the pause must not have split the turn"
    assert calls[0][1] == "When your pain started were you walking or sitting?"
