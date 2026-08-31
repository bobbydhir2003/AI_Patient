"""Phase 5A (EXPERIMENTAL semantic barge-in) - see app/livekit_agent/
turn_detector.py's BargeInDecision/classify_barge_in and worker.py's
_CandidateTurnCoordinator barge-in buffer / PocAgentSession.
_on_semantic_barge_in / _cancel_active_patient_turn.

Reuses the SAME fake-rtc/fake-room fixtures and the Phase D2
_wire_happy_generation/_drive_until_speaking helpers (a real patient turn
genuinely reaching the audio-publish/SPEAKING phase) rather than
duplicating them - semantic barge-in is only ever actionable once a turn
is genuinely speaking (same restriction the manual interrupt button
already has), so these tests need the SAME setup Phase D2's own
interrupt tests use.
"""
import asyncio
import logging

from app.livekit_agent import patient_adapter
from app.livekit_agent.turn_detector import (
    BargeInDecision,
    TurnDecision,
    classify_barge_in,
)
from app.livekit_agent.worker import _CandidateTurnCoordinator
from tests.test_livekit_phase4_semantic_control import _FixedDetector, _feed_boundary
from tests.test_livekit_phase_c import _control_messages, _make_ready_session, _run_until_idle, _turn_statuses
from tests.test_livekit_phase_d2 import _drive_until_speaking, _InterruptPacket, _wire_happy_generation
from tests.test_livekit_poc import _fake_rtc_for_worker


# =================================================================
# Pure classifier (Steps 5/16 items 1-6) - no coordinator, no session.
# =================================================================

def test_classify_mm_hmm_is_acknowledgement():
    assert classify_barge_in("mm-hmm") == BargeInDecision.ACKNOWLEDGEMENT


def test_classify_yeah_is_acknowledgement():
    assert classify_barge_in("yeah") == BargeInDecision.ACKNOWLEDGEMENT


def test_classify_various_ack_words():
    for phrase in ["yep", "okay", "ok", "right", "got it", "uh-huh", "gotcha", "sure", "okay right"]:
        assert classify_barge_in(phrase) == BargeInDecision.ACKNOWLEDGEMENT, phrase


def test_classify_wait_is_true_barge_in():
    assert classify_barge_in("wait") == BargeInDecision.TRUE_BARGE_IN


def test_classify_what_do_you_mean_is_true_barge_in():
    assert classify_barge_in("what do you mean?") == BargeInDecision.TRUE_BARGE_IN


def test_classify_yeah_but_what_do_you_mean_is_eventually_true_barge_in():
    assert classify_barge_in("yeah, but what do you mean?") == BargeInDecision.TRUE_BARGE_IN


def test_classify_tiny_noisy_fragment_is_undecided_not_barge_in():
    for fragment in ["uh", "um", "hm", ""]:
        assert classify_barge_in(fragment) == BargeInDecision.UNDECIDED, fragment


def test_classify_waiting_does_not_false_match_wait():
    # Word-exact matching (Step 6: avoid false positives) - "waiting" must
    # never trigger on the "wait" substring.
    assert classify_barge_in("I am still waiting for something") == BargeInDecision.TRUE_BARGE_IN
    # (still TRUE_BARGE_IN here because it's a long substantive clause, but
    # NOT because "wait" false-matched inside "waiting" - verified directly:)
    from app.livekit_agent.turn_detector import _contains_phrase, normalize_barge_in_text

    words = normalize_barge_in_text("I am still waiting for something").split()
    assert not _contains_phrase(words, "wait")


# =================================================================
# _CandidateTurnCoordinator: barge-in buffer/classification in isolation.
# =================================================================

def _make_barge_in_coordinator(*, decisions=None, on_barge_in, is_patient_speaking=None, on_end=None):
    return _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector(decisions or []), sample_rate=16000,
        on_end=on_end, is_patient_speaking=is_patient_speaking or (lambda: "patient-turn-1"),
        on_barge_in=on_barge_in,
    )


def test_true_barge_in_promotes_a_new_candidate_turn():
    calls = []

    async def on_barge_in(speaking_id, transcript, new_id):
        calls.append((speaking_id, transcript, new_id))

    coordinator = _make_barge_in_coordinator(on_barge_in=on_barge_in)

    async def _drive():
        coordinator.on_speech_started()
        coordinator.on_final_transcript("wait")
        await asyncio.sleep(0.05)

    asyncio.run(_drive())
    assert len(calls) == 1
    speaking_id, transcript, new_id = calls[0]
    assert speaking_id == "patient-turn-1"
    assert transcript == "wait"
    assert new_id.startswith("semantic-sess-1-")
    # Promoted into the coordinator's OWN normal candidate-turn state.
    assert coordinator._candidate_turn_id == new_id
    assert coordinator._candidate_segments == ["wait"]
    assert coordinator._barge_in_buffer == []


def test_acknowledgement_does_not_promote_or_call_on_barge_in():
    calls = []

    async def on_barge_in(speaking_id, transcript, new_id):
        calls.append((speaking_id, transcript, new_id))

    coordinator = _make_barge_in_coordinator(on_barge_in=on_barge_in)

    async def _drive():
        coordinator.on_speech_started()
        coordinator.on_final_transcript("mm-hmm")
        await asyncio.sleep(0.02)

    asyncio.run(_drive())
    assert calls == []
    assert coordinator._candidate_turn_id is None
    assert coordinator._barge_in_buffer == ["mm-hmm"]  # still buffered, unresolved


def test_ambiguous_ack_then_continuation_eventually_barges_in():
    """Step 9: 'yeah' alone stays provisional; once 'but what do you mean'
    arrives in a LATER final for the SAME speech segment, the FULL buffer
    re-classifies to TRUE_BARGE_IN."""
    calls = []

    async def on_barge_in(speaking_id, transcript, new_id):
        calls.append((speaking_id, transcript, new_id))

    coordinator = _make_barge_in_coordinator(on_barge_in=on_barge_in)

    async def _drive():
        coordinator.on_speech_started()
        coordinator.on_final_transcript("yeah")
        await asyncio.sleep(0.01)
        assert calls == []  # not yet - still provisional
        coordinator.on_final_transcript("but what do you mean")
        await asyncio.sleep(0.05)

    asyncio.run(_drive())
    assert len(calls) == 1
    assert calls[0][1] == "yeah but what do you mean"


def test_acknowledgement_discarded_at_speech_ended_never_evaluates_smart_turn():
    """Step 8: an ack that resolves via speech_ended (not continuation)
    must never spend a Smart Turn inference and must never become a
    standalone student turn."""
    end_calls = []

    async def on_end(turn_id, transcript):
        end_calls.append((turn_id, transcript))

    async def barge_in_never_called(*args):
        raise AssertionError("must not be called for a pure acknowledgement")

    coordinator = _make_barge_in_coordinator(on_barge_in=barge_in_never_called, on_end=on_end)

    async def _drive():
        coordinator.on_speech_started()
        coordinator.on_final_transcript("mm-hmm")
        coordinator.on_speech_ended(speech_duration_s=0.4, silence_duration_s=0.6)
        await asyncio.sleep(0.05)

    asyncio.run(_drive())
    assert end_calls == [], "an acknowledgement must never become a submitted student turn"
    assert coordinator._eval_task is None, "Smart Turn must never be invoked for a discarded acknowledgement"
    assert coordinator._barge_in_buffer == []


def test_acknowledgement_does_not_contaminate_next_real_turn():
    end_calls = []

    async def on_end(turn_id, transcript):
        end_calls.append((turn_id, transcript))

    speaking = {"id": "patient-turn-1"}

    async def barge_in_never_called(*args):
        raise AssertionError("mm-hmm must never barge in")

    coordinator = _make_barge_in_coordinator(
        decisions=[TurnDecision.END],
        on_barge_in=barge_in_never_called, on_end=on_end,
        is_patient_speaking=lambda: speaking["id"],
    )

    async def _drive():
        # Ack while patient is (still) speaking - discarded.
        coordinator.on_speech_started()
        coordinator.on_final_transcript("mm-hmm")
        coordinator.on_speech_ended(speech_duration_s=0.4, silence_duration_s=0.6)
        await asyncio.sleep(0.02)

        # Patient stops speaking; student asks a real, complete question.
        speaking["id"] = None
        await _feed_boundary(coordinator, text="Where does it hurt?")

    asyncio.run(_drive())
    assert len(end_calls) == 1
    assert end_calls[0][1] == "Where does it hurt?", "the discarded ack must not leak into the real turn's text"


def test_semantic_end_after_barge_in_submits_exactly_one_student_turn():
    end_calls = []

    async def on_end(turn_id, transcript):
        end_calls.append((turn_id, transcript))

    barge_in_calls = []

    async def on_barge_in(speaking_id, transcript, new_id):
        barge_in_calls.append((speaking_id, transcript, new_id))

    coordinator = _make_barge_in_coordinator(
        decisions=[TurnDecision.END], on_barge_in=on_barge_in, on_end=on_end,
    )

    async def _drive():
        coordinator.on_speech_started()
        coordinator.on_final_transcript("wait")  # promotes
        await asyncio.sleep(0.02)
        # Continue the (now-promoted) candidate turn normally.
        await _feed_boundary(coordinator, text="what do you mean by gradually")

    asyncio.run(_drive())
    assert len(barge_in_calls) == 1
    assert len(end_calls) == 1
    assert end_calls[0][1] == "wait what do you mean by gradually"


def test_barge_in_flag_off_is_byte_for_byte_phase4_behavior():
    """Both is_patient_speaking and on_barge_in are None together whenever
    barge-in is off (see worker.py's _maybe_start_turn_detector wiring) -
    student speech during patient audio is still accumulated normally,
    completely unaffected by Phase 5A's existence."""
    coordinator = _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
        on_end=None, is_patient_speaking=None, on_barge_in=None,
    )

    async def _drive():
        await _feed_boundary(coordinator, text="mm-hmm")

    asyncio.run(_drive())
    # No barge-in gating at all - "mm-hmm" is just ordinary Phase 4
    # candidate-turn text (HOLD preserves it).
    assert coordinator._candidate_segments == ["mm-hmm"]
    assert coordinator._candidate_turn_id is not None


# =================================================================
# PocAgentSession integration: real SPEAKING-phase cancellation, races,
# duplicate protection, fallback (Steps 7/10/16 items 7-10, 16).
# =================================================================

def test_true_barge_in_cancels_patient_playback_exactly_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-a")
            session._semantic_control_active = True
            session._semantic_barge_in_active = True
            coordinator = _CandidateTurnCoordinator(
                session_id=session.session_id, identity="student-1", track_sid="track-1",
                detector=_FixedDetector([]), sample_rate=16000,
                on_end=session._handle_semantic_turn_end,
                is_patient_speaking=session._get_speaking_client_turn_id,
                on_barge_in=session._on_semantic_barge_in,
            )
            coordinator.on_speech_started()
            coordinator.on_final_transcript("wait")
            await _run_until_idle()

        asyncio.run(_drive())

    assert session._audio_source.clear_queue_calls == 1
    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-a", "status": "speaking_started"} in statuses
    assert {"clientTurnId": "turn-a", "status": "interrupted"} in statuses
    assert session._speaking_client_turn_id is None
    assert not session._turn_lock.locked()


def test_duplicate_barge_in_signal_cancels_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-b")
            session._semantic_control_active = True
            session._semantic_barge_in_active = True
            await session._on_semantic_barge_in("turn-b", "wait", "semantic-dup-1")
            await session._on_semantic_barge_in("turn-b", "wait", "semantic-dup-1")

        asyncio.run(_drive())

    assert session._audio_source.clear_queue_calls == 1
    assert len([s for s in _turn_statuses(room) if s == {"clientTurnId": "turn-b", "status": "interrupted"}]) == 1


def test_speaking_already_ended_race_is_a_safe_no_op(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = patient_adapter  # placeholder for readability

        def fake_generate(db, **kwargs):
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            session._semantic_control_active = True
            session._semantic_barge_in_active = True
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                # Nothing ever reached SPEAKING (session generation raised
                # immediately) - a barge-in for a turn id that never spoke.
                await session._on_semantic_barge_in("turn-never-spoke", "wait", "semantic-race-1")

        asyncio.run(_drive())

    assert session._audio_source is not None
    assert session._audio_source.clear_queue_calls == 0
    assert any("semantic_barge_in_race_speaking_already_ended" in r.message for r in caplog.records)


def test_manual_interrupt_and_semantic_barge_in_race_cancels_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-c")
            session._semantic_control_active = True
            session._semantic_barge_in_active = True
            # Both fire for the SAME clientTurnId, effectively simultaneously.
            room.emit("data_received", _InterruptPacket("turn-c"))
            await session._on_semantic_barge_in("turn-c", "wait", "semantic-race-2")
            await _run_until_idle()

        asyncio.run(_drive())

    assert session._audio_source.clear_queue_calls == 1
    assert len([s for s in _turn_statuses(room) if s == {"clientTurnId": "turn-c", "status": "interrupted"}]) == 1


def test_fallback_disables_barge_in_control(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)

        async def _drive():
            await session.start()
            session._semantic_control_active = True
            session._semantic_barge_in_active = True
            session._fallback_to_browser_control("test_forced_failure")

        asyncio.run(_drive())

    assert session._semantic_control_active is False
    assert session._semantic_barge_in_active is False
