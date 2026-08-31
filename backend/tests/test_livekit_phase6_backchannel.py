"""Phase 6 (EXPERIMENTAL patient backchanneling) - see app/livekit_agent/
worker.py's _CandidateTurnCoordinator on_hold/on_student_resumed/
is_backchannel_echo callbacks and PocAgentSession._on_semantic_hold/
_schedule_backchannel/_play_backchannel/_cancel_pending_backchannel/
_is_likely_backchannel_echo, plus patient_adapter.py's
resolve_backchannel_voice_key/synthesize_backchannel_audio_pcm.

A PATIENT_BACKCHANNEL is never a PATIENT_RESPONSE - most PocAgentSession-
level tests here directly assert the ABSENCE of every real-turn side
effect (no generate_and_persist_turn call, no ROLE_PATIENT row, no
patient_turn_status "interrupted", no Phase 5B finalization) alongside the
PRESENCE of the backchannel-specific behavior.
"""
import asyncio
import logging

from app.livekit_agent import patient_adapter
from app.livekit_agent.turn_detector import TurnDecision
from app.livekit_agent.worker import _CandidateTurnCoordinator
from tests.test_livekit_phase4_semantic_control import _FixedDetector, _feed_boundary
from tests.test_livekit_phase_c import _make_ready_session, _run_until_idle, _StudentTextPacket, _turn_statuses
from tests.test_livekit_phase_d2 import _drive_until_speaking, _wire_happy_generation
from tests.test_livekit_poc import _fake_rtc_for_worker
from tests.test_voice import give_carly_a_voice_id

_LONG_ENOUGH = "So when you first noticed the pain"  # 7 words, clears the content bar
_TOO_SHORT = "So um"  # 2 words, below _MIN_BACKCHANNEL_WORDS


def _make_coordinator(
    decisions, *, on_hold=None, on_student_resumed=None, is_backchannel_echo=None, on_end=None,
):
    return _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector(decisions), sample_rate=16000,
        on_end=on_end, on_hold=on_hold,
        on_student_resumed=on_student_resumed, is_backchannel_echo=is_backchannel_echo,
    )


# =================================================================
# Coordinator-level: content filter, HOLD-vs-END, echo guard.
# =================================================================

def test_end_never_calls_on_hold():
    calls = []
    coordinator = _make_coordinator([TurnDecision.END], on_hold=lambda *a: calls.append(a))

    async def _drive():
        await _feed_boundary(coordinator, text="Where does it hurt?")

    asyncio.run(_drive())
    assert calls == []


def test_hold_with_enough_content_calls_on_hold():
    calls = []
    coordinator = _make_coordinator([TurnDecision.HOLD], on_hold=lambda *a: calls.append(a))

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)

    asyncio.run(_drive())
    assert len(calls) == 1
    candidate_turn_id, transcript, probability = calls[0]
    assert candidate_turn_id == coordinator._candidate_turn_id
    assert transcript == _LONG_ENOUGH
    assert probability == 0.9  # _FixedDetector's fixed probability


def test_hold_with_tiny_fragment_does_not_call_on_hold():
    calls = []
    coordinator = _make_coordinator([TurnDecision.HOLD], on_hold=lambda *a: calls.append(a))

    async def _drive():
        await _feed_boundary(coordinator, text=_TOO_SHORT)

    asyncio.run(_drive())
    assert calls == []


def test_on_hold_never_mutates_candidate_state():
    """Step 14: calling on_hold itself must be a pure notification - the
    coordinator's own candidate_segments/turn_id are untouched by it."""
    calls = []
    coordinator = _make_coordinator([TurnDecision.HOLD], on_hold=lambda *a: calls.append(a))

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)

    asyncio.run(_drive())
    assert len(calls) == 1
    assert coordinator._candidate_segments == [_LONG_ENOUGH]
    assert coordinator._candidate_turn_id is not None


def test_continuation_after_hold_then_end_submits_full_merged_transcript():
    end_calls = []
    hold_calls = []

    async def on_end(turn_id, transcript):
        end_calls.append((turn_id, transcript))

    coordinator = _make_coordinator(
        [TurnDecision.HOLD, TurnDecision.END],
        on_hold=lambda *a: hold_calls.append(a),
        on_end=on_end,
    )

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)
        await _feed_boundary(coordinator, text="were you walking when it started?")

    asyncio.run(_drive())
    assert len(hold_calls) == 1
    assert len(end_calls) == 1
    assert end_calls[0][1] == f"{_LONG_ENOUGH} were you walking when it started?"


def test_backchannel_echo_is_discarded_not_accumulated():
    coordinator = _make_coordinator(
        [TurnDecision.HOLD], is_backchannel_echo=lambda text: text.strip().lower() == "mm-hmm",
    )

    async def _drive():
        coordinator.on_final_transcript("Mm-hmm")

    asyncio.run(_drive())
    assert coordinator._candidate_segments == []


def test_substantive_speech_during_echo_guard_is_preserved():
    coordinator = _make_coordinator(
        [TurnDecision.HOLD], is_backchannel_echo=lambda text: text.strip().lower() == "mm-hmm",
    )

    async def _drive():
        coordinator.on_final_transcript("okay, but when did the pain start?")

    asyncio.run(_drive())
    assert coordinator._candidate_segments == ["okay, but when did the pain start?"]


def test_flag_off_is_byte_for_byte_phase5b_behavior():
    """on_hold/on_student_resumed/is_backchannel_echo all None together
    whenever backchanneling is off (see worker.py's wiring) - HOLD behaves
    exactly as Phase 4/5A/5B already did."""
    coordinator = _CandidateTurnCoordinator(
        session_id="sess-1", identity="student-1", track_sid="track-1",
        detector=_FixedDetector([TurnDecision.HOLD]), sample_rate=16000,
    )

    async def _drive():
        await _feed_boundary(coordinator, text=_LONG_ENOUGH)

    asyncio.run(_drive())
    assert coordinator._candidate_segments == [_LONG_ENOUGH]


# =================================================================
# PocAgentSession-level: real scheduling/cancellation/playback/eligibility.
# =================================================================

def _make_backchannel_session(engine, monkeypatch, *, delay_seconds=0.05):
    """A ready PocAgentSession with backchanneling forced on and the
    real post-HOLD delay shrunk to keep tests fast (the delay itself is
    the ONLY thing shortened - every other code path runs for real)."""
    session, room, sid = _make_ready_session(engine, monkeypatch)
    monkeypatch.setattr("app.livekit_agent.worker._BACKCHANNEL_HOLD_DELAY_SECONDS", delay_seconds)
    give_carly_a_voice_id(monkeypatch)
    fake_pcm = b"\x00\x01" * 800  # 16-bit mono @16kHz -> 50ms of audio
    monkeypatch.setattr(
        patient_adapter, "synthesize_backchannel_audio_pcm",
        lambda **kwargs: fake_pcm,
    )
    return session, room, sid


async def _start_with_backchannel(session):
    await session.start()
    session._semantic_control_active = True
    session._backchannel_enabled = True


def test_student_resumes_before_delay_cancels_pending_backchannel(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.3)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.05)  # well before the 0.3s delay elapses
            session._cancel_pending_backchannel()
            await asyncio.sleep(0.4)  # past the original delay - must NOT have played

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is None
    assert session._audio_source.captured_frames == 0


def test_student_stays_silent_one_backchannel_plays(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.2)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id == "semantic-x-1"
    assert session._audio_source.captured_frames > 0
    assert session._audio_source.clear_queue_calls == 0  # completed naturally, never cancelled


def test_backchannel_never_calls_handle_student_turn_or_persists(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)
        generate_calls = []

        def fake_generate(db, **kwargs):
            generate_calls.append(kwargs)
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.2)

        asyncio.run(_drive())

    assert generate_calls == [], "a backchannel must never trigger patient generation"
    assert session._active_client_turn_id is None
    assert _turn_statuses(room) == []


def test_max_one_backchannel_per_semantic_turn(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.15)
            first_frames = session._audio_source.captured_frames
            # A SECOND HOLD for the SAME candidate_turn_id (e.g. the student
            # paused again mid-continuation) must be ineligible.
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH + " and", 0.9)
            await asyncio.sleep(0.15)
            return first_frames

        first_frames = asyncio.run(_drive())

    assert first_frames > 0
    assert session._audio_source.captured_frames == first_frames, "no second clip must have played"


def test_next_semantic_turn_is_eligible_again(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.15)
            session._on_semantic_hold("semantic-x-2", _LONG_ENOUGH, 0.9)  # a NEW candidate turn id
            await asyncio.sleep(0.15)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id == "semantic-x-2"


def test_no_backchannel_while_real_patient_turn_thinking_or_speaking(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)

        async def _drive():
            await _start_with_backchannel(session)
            session._active_client_turn_id = "some-real-turn"  # thinking OR speaking
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.15)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is None
    assert session._audio_source.captured_frames == 0


def test_student_resumes_while_backchannel_playing_stops_it(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)

        async def _drive():
            await _start_with_backchannel(session)
            block_event = asyncio.Event()
            session._audio_source.block_on_frame = 1
            session._audio_source.block_event = block_event
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if session._backchannel_playing:
                    break
            assert session._backchannel_playing, "backchannel never reached the audio-publish phase"
            session._cancel_pending_backchannel()
            await asyncio.sleep(0.05)

        asyncio.run(_drive())

    assert session._audio_source.clear_queue_calls == 1
    assert session._backchannel_playing is False
    # Step 18: no real patient-turn interruption lifecycle for a backchannel.
    assert _turn_statuses(room) == []


def test_cancelling_backchannel_does_not_invoke_phase5b_finalization(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.3)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.05)
            session._cancel_pending_backchannel()
            await asyncio.sleep(0.05)

        asyncio.run(_drive())

    assert len(session._finalized_patient_turn_ids) == 0, "nothing to finalize - a backchannel is never a real turn"


def test_voice_unavailable_skips_backchannel(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)
        monkeypatch.setattr(patient_adapter, "resolve_backchannel_voice_key", lambda case_id: None)

        async def _drive():
            await _start_with_backchannel(session)
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.15)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is None
    assert session._audio_source.captured_frames == 0


def test_synthesis_failure_is_fail_open(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)
        monkeypatch.setattr(patient_adapter, "synthesize_backchannel_audio_pcm", lambda **kwargs: None)

        async def _drive():
            await _start_with_backchannel(session)
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
                await asyncio.sleep(0.15)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is None
    assert session._audio_source.captured_frames == 0
    assert any("patient_backchannel_failed" in r.message for r in caplog.records)
    assert not session._shutdown_called, "a backchannel failure must never affect session health"


def test_real_patient_response_still_speaking_blocks_backchannel(monkeypatch, engine):
    """Full integration: a REAL patient turn is genuinely in the audio-
    publish phase (not just a fake _active_client_turn_id) when a HOLD
    fires for a (hypothetically concurrent) student utterance."""
    with _fake_rtc_for_worker():
        session, room, sid = _make_backchannel_session(engine, monkeypatch, delay_seconds=0.03)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-a")
            session._semantic_control_active = True
            session._backchannel_enabled = True
            session._on_semantic_hold("semantic-x-1", _LONG_ENOUGH, 0.9)
            await asyncio.sleep(0.15)

        asyncio.run(_drive())

    assert session._backchannel_played_turn_id is None


def test_flag_off_no_backchannel_state_wired(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)

        async def _drive():
            await session.start()

        asyncio.run(_drive())

    assert session._backchannel_enabled is False
