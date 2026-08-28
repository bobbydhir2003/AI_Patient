"""Phase D2: true SPEAKING-only patient interruption ("barge-in") - see
app/livekit_agent/worker.py's PocAgentSession class docstring for the
THINKING-vs-SPEAKING rationale (OpenAI/ElevenLabs run in a thread pool that
cannot be forcibly stopped, so real cancellation is only offered once audio
is actually publishing), and src/services/livekit/livekitPocEngine.ts's
interruptPatient()/handleTurnStatus for the matching frontend half (covered
by scripts/test-livekit-poc.mjs).

Reuses the SAME fake-rtc/fake-room fixtures test_livekit_poc.py/
test_livekit_phase_c.py already established, extended there with
AudioSource.clear_queue()/captured_frames/block_on_frame so a turn's audio
publish can be suspended mid-stream to prove a REAL (not just fast-race)
cancellation.
"""
import asyncio
import json
import logging

from app.livekit_agent import patient_adapter
from app.livekit_agent.worker import _FRAME_BYTES
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_phase_c import (
    _control_messages,
    _make_ready_session,
    _run_until_idle,
    _StudentTextPacket,
    _turn_statuses,
)
from tests.test_livekit_poc import _fake_rtc_for_worker
from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id


class _InterruptPacket:
    topic = "agent_control"

    def __init__(self, client_turn_id: str):
        self.data = json.dumps({"type": "interrupt_patient", "clientTurnId": client_turn_id}).encode()


def _wire_happy_generation(monkeypatch, *, text="I've had it for two days.", frames: int = 5):
    """Wires OpenAI + ElevenLabs so a turn reaches the audio-publish phase
    with `frames` distinct 20ms PCM frames (see worker.py's _FRAME_BYTES) -
    enough for a test to block mid-stream and prove interruption actually
    stops further publication, not just that it raced a single instant
    write."""
    fake_openai = FakeOpenAIClient(text=text)
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
    give_carly_a_voice_id(monkeypatch)
    fake_el = FakeElevenLabsClient(chunks=(b"\x00\x01" * (_FRAME_BYTES * frames // 2),))
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)
    return fake_openai, fake_el


async def _drive_until_speaking(session, room, text, client_turn_id, *, block_on_frame=1):
    """Starts a turn and suspends it exactly at the Nth captured audio frame
    (default: the very first) so the test can interrupt a GENUINELY
    in-flight, currently-speaking turn - not one that merely completed
    before the interrupt message could be processed."""
    await session.start()
    block_event = asyncio.Event()
    # _audio_source only exists once start() has published the track.
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
# A: active turn task is stored
# =================================================================

def test_active_turn_task_and_id_are_stored_while_a_turn_runs(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-a")
            assert session._active_turn_task is not None
            assert not session._active_turn_task.done()
            assert session._active_client_turn_id == "turn-a"

        asyncio.run(_drive())


# =================================================================
# B/D/H/I/J: a matching interrupt cancels the correct task, clears the
# audio queue, produces "interrupted" (not "failed"), and leaves the
# session in a clean, reusable state.
# =================================================================

def test_matching_interrupt_cancels_task_clears_queue_and_reports_interrupted(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            block_event = await _drive_until_speaking(session, room, "How long?", "turn-b")
            assert session._audio_source.captured_frames == 1, "must be suspended after exactly one frame"

            room.emit("data_received", _InterruptPacket("turn-b"))
            await _run_until_idle()
            return block_event

        asyncio.run(_drive())

    # H: interrupted, never failed (speaking_started is always sent first,
    # unconditionally, the moment audio publish begins - before any
    # interrupt could possibly arrive).
    assert _turn_statuses(room) == [
        {"clientTurnId": "turn-b", "status": "speaking_started"},
        {"clientTurnId": "turn-b", "status": "interrupted"},
    ]
    # D: the queue was cleared exactly once.
    assert session._audio_source.clear_queue_calls == 1
    # Further frames past the one already in flight when cancelled must
    # never be published - proves this is a REAL stop, not just a flag.
    assert session._audio_source.captured_frames == 1
    # I: the turn lock was released, not left stuck busy.
    assert not session._turn_lock.locked()
    # J: active-task bookkeeping fully cleared.
    assert session._active_turn_task is None
    assert session._active_client_turn_id is None
    assert session._speaking_client_turn_id is None
    # G/F: the job/session itself is untouched - no shutdown was triggered
    # merely by interrupting a turn.
    assert session._shutdown_called is False


def _wire_happy_generation_camden(monkeypatch, *, text="He's been sleeping more than usual.", frames: int = 5):
    """Camden variant of _wire_happy_generation: Camden's checked-in case
    file already ships a real (non-placeholder) caregiver voice id, so no
    give_*_a_voice_id override is needed - proves the caregiver voice path
    (fixed by the Camden speaker/voice-key routing change) is just as
    interruptible as the plain single-speaker ("patient") path."""
    fake_openai = FakeOpenAIClient(text=text)
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
    fake_el = FakeElevenLabsClient(chunks=(b"\x00\x01" * (_FRAME_BYTES * frames // 2),))
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)
    return fake_openai, fake_el


def test_camden_interrupt_still_cancels_task_and_clears_queue(monkeypatch, engine):
    """Regression for the Camden caregiver-voice routing fix: D2's
    interrupt/cancellation mechanics operate purely on the audio-publish
    phase (_speaking_client_turn_id/_active_turn_task), never on which
    participant's voice is speaking - proves Camden's caregiver-voiced turns
    can be interrupted exactly like any other case's, post-fix."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, case_id="camden")
        _wire_happy_generation_camden(monkeypatch, frames=5)

        async def _drive():
            block_event = await _drive_until_speaking(
                session, room, "What's been going on?", "camden-turn-b"
            )
            assert session._audio_source.captured_frames == 1
            room.emit("data_received", _InterruptPacket("camden-turn-b"))
            await _run_until_idle()
            return block_event

        asyncio.run(_drive())

    assert _turn_statuses(room) == [
        {"clientTurnId": "camden-turn-b", "status": "speaking_started"},
        {"clientTurnId": "camden-turn-b", "status": "interrupted"},
    ]
    assert session._audio_source.clear_queue_calls == 1
    assert session._audio_source.captured_frames == 1
    assert session._speaking_client_turn_id is None
    assert session._shutdown_called is False


# =================================================================
# C: a mismatched clientTurnId never cancels the active turn
# =================================================================

def test_mismatched_client_turn_id_does_not_cancel(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            block_event = await _drive_until_speaking(session, room, "How long?", "turn-c")
            room.emit("data_received", _InterruptPacket("some-other-turn"))
            await _run_until_idle()
            assert session._active_turn_task is not None
            assert not session._active_turn_task.done()
            assert session._audio_source.clear_queue_calls == 0
            # Let the real turn finish normally.
            block_event.set()
            await _run_until_idle()

        asyncio.run(_drive())

    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-c", "status": "speaking_started"} in statuses
    assert {"clientTurnId": "turn-c", "status": "speaking_ended"} in statuses
    assert {"clientTurnId": "turn-c", "status": "interrupted"} not in statuses


# =================================================================
# An interrupt for a turn still THINKING (not yet speaking) is a safe
# no-op - see the class docstring's THINKING-vs-SPEAKING rationale
# (requirement 1/18: never fake-cancel work OpenAI/ElevenLabs is still
# doing in the executor thread pool).
# =================================================================

def test_interrupt_before_speaking_started_is_a_stale_noop(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        # This test's final assertion expects the turn to reach a real
        # "speaking_ended" (the interrupt arrives too early to cancel
        # anything - see the module docstring) - mock ElevenLabs exactly like
        # every sibling test in this file (_wire_happy_generation) so that
        # happens deterministically, with no dependency on a real provider
        # key/network call.
        give_carly_a_voice_id(monkeypatch)
        fake_el = FakeElevenLabsClient(chunks=(b"\x00\x01",))
        monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

        # Block INSIDE generate_and_persist_turn (the THINKING phase, running
        # in the executor thread pool) using plain threading primitives -
        # this deliberately does NOT touch the event loop, matching how the
        # real OpenAI call would block a worker thread.
        import threading
        thinking_entered = threading.Event()
        allow_thinking_to_finish = threading.Event()
        original_generate = patient_adapter.generate_and_persist_turn

        def blocking_generate(db, **kwargs):
            thinking_entered.set()
            allow_thinking_to_finish.wait(timeout=5)
            return original_generate(db, **kwargs)

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", blocking_generate)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Hi", "turn-thinking"))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if thinking_entered.is_set():
                    break
            assert thinking_entered.is_set(), "expected to reach the THINKING phase"
            assert session._active_client_turn_id == "turn-thinking"
            assert session._speaking_client_turn_id is None

            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _InterruptPacket("turn-thinking"))
                await _run_until_idle()

            assert session._active_turn_task is not None
            assert not session._active_turn_task.done(), "a THINKING-phase interrupt must never cancel the task"

            allow_thinking_to_finish.set()
            await _run_until_idle()

        asyncio.run(_drive())

    assert any("livekit_agent_interrupt_stale" in r.message and "turn-thinking" in r.message for r in caplog.records)
    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-thinking", "status": "interrupted"} not in statuses
    assert {"clientTurnId": "turn-thinking", "status": "speaking_ended"} in statuses


# =================================================================
# K: double interrupt is safe/idempotent
# =================================================================

def test_double_interrupt_is_idempotent(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-k")
            room.emit("data_received", _InterruptPacket("turn-k"))
            room.emit("data_received", _InterruptPacket("turn-k"))
            room.emit("data_received", _InterruptPacket("turn-k"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _turn_statuses(room) == [
        {"clientTurnId": "turn-k", "status": "speaking_started"},
        {"clientTurnId": "turn-k", "status": "interrupted"},
    ]
    assert session._audio_source.clear_queue_calls == 1


# =================================================================
# A stale interrupt for an OLD, already-naturally-completed turn must
# never cancel a NEWER turn (requirement 5/17.C).
# =================================================================

def test_stale_interrupt_for_a_completed_turn_never_cancels_a_newer_one(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=1)

        async def _drive():
            # Turn A: runs to natural completion (never blocked).
            await session.start()
            room.emit("data_received", _StudentTextPacket("First question", "turn-old"))
            await _run_until_idle()
            assert {"clientTurnId": "turn-old", "status": "speaking_ended"} in _turn_statuses(room)

            # Turn B starts, and is currently speaking when the STALE
            # interrupt for A finally arrives.
            block_event = await _drive_until_speaking(session, room, "Second question", "turn-new")
            room.emit("data_received", _InterruptPacket("turn-old"))
            await _run_until_idle()
            assert session._active_client_turn_id == "turn-new"
            assert not session._active_turn_task.done()
            block_event.set()
            await _run_until_idle()

        asyncio.run(_drive())

    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-new", "status": "speaking_ended"} in statuses
    assert {"clientTurnId": "turn-new", "status": "interrupted"} not in statuses
    assert {"clientTurnId": "turn-old", "status": "interrupted"} not in statuses


# =================================================================
# M: the NEXT turn after an interrupt still processes normally
# =================================================================

def test_turn_after_an_interrupt_processes_normally(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-m1")
            room.emit("data_received", _InterruptPacket("turn-m1"))
            await _run_until_idle()

            # A brand new turn, unblocked this time, must run end-to-end.
            session._audio_source.block_on_frame = None
            room.emit("data_received", _StudentTextPacket("Anything else?", "turn-m2"))
            await _run_until_idle()

        asyncio.run(_drive())

    statuses = _turn_statuses(room)
    assert {"clientTurnId": "turn-m1", "status": "interrupted"} in statuses
    assert {"clientTurnId": "turn-m2", "status": "speaking_started"} in statuses
    assert {"clientTurnId": "turn-m2", "status": "speaking_ended"} in statuses


# =================================================================
# N: an interrupted clientTurnId is terminal - a resend must not
# regenerate the patient response from scratch (dedup/idempotency).
# =================================================================

def test_interrupted_turn_id_is_not_regenerated_on_resend(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai, _fake_el = _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session, room, "How long?", "turn-n")
            room.emit("data_received", _InterruptPacket("turn-n"))
            await _run_until_idle()
            assert len(fake_openai.calls) == 1

            # SAME clientTurnId resent (e.g. a browser-side ack-timeout retry
            # racing the interrupt) - must be ack'd but never reprocessed.
            room.emit("data_received", _StudentTextPacket("How long?", "turn-n"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert len(fake_openai.calls) == 1, "an interrupted turn must never be regenerated on a duplicate"
    acks = _control_messages(room, "turn_ack")
    assert len(acks) == 2, "every receipt is still ack'd, even a duplicate-after-interrupt"


# =================================================================
# O: two different PocAgentSession jobs never affect each other's
# active-turn bookkeeping (Phase D2 requirement: job-local only, no
# global registries).
# =================================================================

def test_two_sessions_have_independent_active_turn_state(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session_a, room_a, _sid_a = _make_ready_session(engine, monkeypatch, remote_identities={"student-a": object()})
        session_b, room_b, _sid_b = _make_ready_session(engine, monkeypatch, remote_identities={"student-b": object()})
        _wire_happy_generation(monkeypatch, frames=5)

        async def _drive():
            await _drive_until_speaking(session_a, room_a, "Question A", "turn-o-a")
            await session_b.start()
            assert session_b._active_turn_task is None
            assert session_b._active_client_turn_id is None
            assert session_b._speaking_client_turn_id is None

            # An interrupt sent to session B's room for session A's turn id
            # must be meaningless - session B never even saw A's turn.
            room_b.emit("data_received", _InterruptPacket("turn-o-a"))
            await _run_until_idle()
            assert session_a._active_turn_task is not None
            assert not session_a._active_turn_task.done()

        asyncio.run(_drive())
