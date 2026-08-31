"""Phase 5B (EXPERIMENTAL spoken-transcript sync) - see app/livekit_agent/
patient_adapter.py's split_into_sentences/finalize_partial_patient_delivery
and worker.py's PocAgentSession._run_turn per-sentence branch/
_finalize_partial_patient_delivery.

Reuses the SAME fake-rtc/fake-room fixtures and Phase D2's
_wire_happy_generation/_drive_until_speaking helpers - transcript-
integrity only matters once a turn is genuinely mid-speech, exactly the
setup Phase D2's own interrupt tests already need. Unlike the Phase 4/5A
test files, most tests here do NOT monkeypatch generate_and_persist_turn -
they let the REAL DB-backed insert/update path run, then read the
persisted ConversationTurn row back to verify content, since that DB
content is the entire point of this phase.
"""
import asyncio
import logging

from sqlalchemy.orm import sessionmaker

from app.livekit_agent import patient_adapter
from app.livekit_agent.worker import _FRAME_BYTES
from app.repositories.transcript_repository import TranscriptRepository
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_phase_c import _make_ready_session, _run_until_idle, _StudentTextPacket, _turn_statuses
from tests.test_livekit_phase_d2 import _InterruptPacket, _wire_happy_generation
from tests.test_livekit_poc import _fake_rtc_for_worker
from tests.test_voice import give_carly_a_voice_id

TWO_SENTENCES = "The pain started three weeks ago. Walking makes it worse."
THREE_SENTENCES = "The pain started three weeks ago. It has gotten worse. Walking makes it hurt more."


def _patient_row(engine, session_id):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        return TranscriptRepository(db).get_by_index(session_id, 1)
    finally:
        db.close()


async def _drive_until_speaking_sync_mode(session, room, text, client_turn_id, *, block_on_frame=1):
    """Identical to test_livekit_phase_d2._drive_until_speaking, except the
    Phase 5B flag is flipped on right after start() (before the turn is
    even submitted) - _run_turn reads it once per turn, so it must be set
    before the student_text packet is emitted."""
    await session.start()
    session._spoken_transcript_sync_active = True
    block_event = asyncio.Event()
    session._audio_source.block_on_frame = block_on_frame
    session._audio_source.block_event = block_event
    room.emit("data_received", _StudentTextPacket(text, client_turn_id))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if session._speaking_client_turn_id == client_turn_id:
            break
    assert session._speaking_client_turn_id == client_turn_id, "turn never reached the audio-publish phase"
    return block_event


# =================================================================
# Pure sentence splitter
# =================================================================

def test_split_into_sentences_basic():
    assert patient_adapter.split_into_sentences(TWO_SENTENCES) == [
        "The pain started three weeks ago.", "Walking makes it worse.",
    ]


def test_split_into_sentences_single_sentence_no_terminal_punctuation():
    assert patient_adapter.split_into_sentences("Okay") == ["Okay"]


def test_split_into_sentences_empty():
    assert patient_adapter.split_into_sentences("   ") == []


# =================================================================
# 1/9: normal, non-interrupted completion - full transcript, unchanged
# behavior between flag on and flag off.
# =================================================================

def test_normal_full_playback_persists_full_transcript_exactly_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=2)

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-normal"))
            await _run_until_idle(80)

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == TWO_SENTENCES
    assert patient_turn.validation_status != "interrupted"
    assert patient_turn.validation_status != "delivery_failed"
    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-normal", "status": "speaking_started"} in statuses
    assert {"clientTurnId": "turn-normal", "status": "speaking_ended"} in statuses
    assert {"clientTurnId": "turn-normal", "status": "interrupted"} not in statuses


def test_flag_off_full_playback_is_unchanged_from_pre_phase5b_behavior(monkeypatch, engine):
    """Same scenario as above, flag OFF (default) - proves zero regression
    for the common/default path."""
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=2)
        assert session._spoken_transcript_sync_active is False

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("How long?", "turn-off"))
            await _run_until_idle(60)

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == TWO_SENTENCES
    assert patient_turn.validation_status == "valid"


# =================================================================
# 2/3/4: interruption at various points - only genuinely-published
# sentences remain authoritative.
# =================================================================

def test_interrupt_before_any_audio_spoken_commits_nothing(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=3)

        async def _drive():
            await _drive_until_speaking_sync_mode(session, room, "How long?", "turn-c", block_on_frame=1)
            room.emit("data_received", _InterruptPacket("turn-c"))
            await _run_until_idle()

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "", "no full generated response should appear as if heard"
    assert patient_turn.validation_status == "interrupted"


def test_first_sentence_spoken_second_interrupted(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=3)

        async def _drive():
            # Sentence 1 = 3 frames; blocking on frame 4 means sentence 1's
            # _publish_pcm call fully completed before we ever suspend.
            await _drive_until_speaking_sync_mode(session, room, "How long?", "turn-d", block_on_frame=4)
            room.emit("data_received", _InterruptPacket("turn-d"))
            await _run_until_idle()

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "The pain started three weeks ago."
    assert "Walking makes it worse" not in patient_turn.content
    assert patient_turn.validation_status == "interrupted"


def test_multiple_completed_sentences_then_interruption(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=THREE_SENTENCES, frames=2)

        async def _drive():
            # Sentences 1+2 = 4 frames total; block on frame 5 (first frame
            # of sentence 3).
            await _drive_until_speaking_sync_mode(session, room, "How long?", "turn-e", block_on_frame=5)
            room.emit("data_received", _InterruptPacket("turn-e"))
            await _run_until_idle()

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "The pain started three weeks ago. It has gotten worse."
    assert "Walking" not in patient_turn.content
    assert patient_turn.validation_status == "interrupted"


# =================================================================
# 5: manual interrupt and semantic barge-in produce the SAME result.
# =================================================================

def test_manual_and_semantic_interrupt_produce_same_transcript_result(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session_a, room_a, sid_a = _make_ready_session(engine, monkeypatch, remote_identities={"student-a": object()})
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=3)

        async def _drive_manual():
            await _drive_until_speaking_sync_mode(session_a, room_a, "How long?", "turn-manual", block_on_frame=4)
            room_a.emit("data_received", _InterruptPacket("turn-manual"))
            await _run_until_idle()

        asyncio.run(_drive_manual())

    with _fake_rtc_for_worker():
        session_b, room_b, sid_b = _make_ready_session(engine, monkeypatch, remote_identities={"student-b": object()})
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=3)

        async def _drive_semantic():
            await _drive_until_speaking_sync_mode(session_b, room_b, "How long?", "turn-semantic", block_on_frame=4)
            session_b._semantic_control_active = True
            session_b._semantic_barge_in_active = True
            await session_b._on_semantic_barge_in("turn-semantic", "wait", "semantic-x-1")
            await _run_until_idle()

        asyncio.run(_drive_semantic())

    row_a = _patient_row(engine, sid_a)
    row_b = _patient_row(engine, sid_b)
    assert row_a.content == row_b.content == "The pain started three weeks ago."
    assert row_a.validation_status == row_b.validation_status == "interrupted"


# =================================================================
# 6/7/8: duplicate/race/idempotency
# =================================================================

def test_duplicate_interrupt_finalizes_transcript_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=3)

        async def _drive():
            await _drive_until_speaking_sync_mode(session, room, "How long?", "turn-dup", block_on_frame=4)
            room.emit("data_received", _InterruptPacket("turn-dup"))
            room.emit("data_received", _InterruptPacket("turn-dup"))
            await _run_until_idle()

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "The pain started three weeks ago."
    assert patient_turn.validation_status == "interrupted"


def test_speaking_ended_before_interrupt_is_a_stale_no_op_full_content_preserved(monkeypatch, engine):
    """Race: the turn finishes NATURALLY (all sentences spoken) before a
    late interrupt arrives - _cancel_active_patient_turn already treats
    this as stale (Phase D2), so no finalize ever runs and the full,
    correctly-delivered content must survive untouched."""
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=2)

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-race"))
            await _run_until_idle(80)  # let it finish naturally, unblocked
            room.emit("data_received", _InterruptPacket("turn-race"))  # late, stale
            await _run_until_idle()

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == TWO_SENTENCES
    statuses = [s["status"] for s in _turn_statuses(room) if s["clientTurnId"] == "turn-race"]
    assert statuses == ["speaking_started", "speaking_ended"]


def test_finalize_partial_patient_delivery_is_idempotent(monkeypatch, engine, caplog):
    """A direct, isolated test of the Step 13 dedup guard - proves a
    duplicate/stale finalize attempt for the SAME patient_turn_id can never
    overwrite already-correct content a second time."""
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Two weeks.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()

        asyncio.run(_drive())

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    result = patient_adapter.generate_and_persist_turn(
        db, session_id=sid, case_id="carly", question="How long?", client_turn_id="turn-idem",
    )
    db.close()

    async def _finalize_twice():
        await session._finalize_partial_patient_delivery(
            result, ["First sentence."], client_turn_id="turn-idem", reason="interrupted",
        )
        await session._finalize_partial_patient_delivery(
            result, ["Something else entirely."], client_turn_id="turn-idem", reason="interrupted",
        )

    with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
        asyncio.run(_finalize_twice())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "First sentence.", "the SECOND finalize call must be a no-op"
    assert any("patient_transcript_finalize_duplicate_ignored" in r.message for r in caplog.records)


# =================================================================
# 10/11/12: generation/TTS/publish failure safety
# =================================================================

def test_generation_failure_before_speaking_persists_nothing(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(fail=True)
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-genfail"))
            await _run_until_idle(60)

        asyncio.run(_drive())

    assert _patient_row(engine, sid) is None, "no fake spoken transcript when generation itself failed"
    assert {"clientTurnId": "turn-genfail", "status": "failed"} in _turn_statuses(room)


def test_mid_response_tts_failure_marks_only_delivered_units_spoken(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text=TWO_SENTENCES)
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
        give_carly_a_voice_id(monkeypatch)

        call_count = {"n": 0}

        def fake_synth(*, case_id, text, voice_key, on_stage=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return b"\x00\x01" * (_FRAME_BYTES // 2)
            return None  # simulated ElevenLabs failure on the second sentence

        monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm", fake_synth)

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-ttsfail"))
            await _run_until_idle(80)

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "The pain started three weeks ago."
    assert patient_turn.validation_status == "delivery_failed"
    assert {"clientTurnId": "turn-ttsfail", "status": "failed"} in _turn_statuses(room)


def test_audio_publish_exception_marks_only_delivered_units_spoken(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=2)

        original_publish = session._publish_pcm
        call_count = {"n": 0}

        async def flaky_publish(pcm):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated audio publish failure")
            await original_publish(pcm)

        session._publish_pcm = flaky_publish

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-pubfail"))
            await _run_until_idle(80)

        asyncio.run(_drive())

    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == "The pain started three weeks ago."
    assert patient_turn.validation_status == "delivery_failed"
    assert {"clientTurnId": "turn-pubfail", "status": "failed"} in _turn_statuses(room)


# =================================================================
# 13: existing idempotent replay still works
# =================================================================

def test_idempotent_replay_still_works_with_flag_on(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        fake_openai, _fake_el = _wire_happy_generation(monkeypatch, text=TWO_SENTENCES, frames=2)

        async def _drive():
            await session.start()
            session._spoken_transcript_sync_active = True
            room.emit("data_received", _StudentTextPacket("How long?", "turn-replay"))
            await _run_until_idle(80)
            room.emit("data_received", _StudentTextPacket("How long?", "turn-replay"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert len(fake_openai.calls) == 1, "a completed turn must never be regenerated on a duplicate"
    patient_turn = _patient_row(engine, sid)
    assert patient_turn.content == TWO_SENTENCES
