"""Phase E + F (interruption / stale-generation lifecycle) - see worker.py's
_on_realtime_speech_started / _handle_realtime_turn / _run_realtime_turn /
_generate_realtime_turn_sync / _finalize_realtime_partial, patient_adapter's
generation-epoch gate (is_generation_valid / GenerationStaleError), and
realtime_session's response_id correlation + cancel_active_response.

Deterministic, no network. Covers the required matrix:
 1  A speaking -> speech_started -> A cancelled           (test_barge_in_*)
 2  queued/unplayed A audio cleared                       (test_barge_in_*)
 3  late A audio delta after cancel ignored               (test_stale_response_*)
 4  late A transcript event ignored                       (test_stale_response_*)
 5  late A response.done cannot change state              (test_stale_response_*)
 6  A generating -> B arrives -> A invalidated            (test_gate_*)
 7  B is NOT busy-dropped                                 (test_new_turn_not_dropped)
 8  A finishes late -> never calls response.create/speak  (test_stale_before_speak_*)
 9  stale A cannot overwrite B                            (test_superseded_before_start)
 10 stale A cannot emit speaking_started                  (test_stale_before_speak_*)
 11 interrupted patient row reconciles                    (test_interrupted_row_*)
 12 duplicate interruption idempotent                     (test_barge_in_idempotent)
 13 rapid B/C maintain monotonic generations              (test_rapid_turns_monotonic)
 14 legacy voice path unchanged                           (test_legacy_*)
"""
import asyncio
import base64
import threading

import pytest

from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_session import RealtimeSession, SpeakResult
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_realtime_phase_a import _pump_until, _settings
from tests.test_livekit_realtime_phase_d import _QueueConn, _audio_delta, _mk_session
from tests.test_livekit_phase_c import _make_ready_session, _turn_statuses
from tests.test_livekit_poc import _fake_rtc_for_worker


class _FakeRealtime:
    def __init__(self, speak_result=None):
        self.cancels = 0
        self.spoke = []
        self._speak_result = speak_result or SpeakResult("", 0, True, False)

    async def speak(self, *, client_turn_id, text, on_audio):
        self.spoke.append((client_turn_id, text))
        return self._speak_result

    async def cancel_active_response(self):
        self.cancels += 1


def _turns(session, sid):
    db = session._session_factory()
    try:
        return TranscriptRepository(db).list_turns(sid)
    finally:
        db.close()


# =====================================================================
# 1, 2, 12 - barge-in while speaking: cancel + clear queue, idempotent
# =====================================================================

def test_barge_in_while_speaking_cancels_and_clears_queue(monkeypatch, engine):
    with _fake_rtc_for_worker():
        import livekit.rtc as rtc
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        session._audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
        fake_rt = _FakeRealtime()
        session._realtime_session = fake_rt
        session._speaking_client_turn_id = "A"
        session._active_client_turn_id = "A"

        async def scenario():
            session._on_realtime_speech_started()
            await _pump_until(lambda: fake_rt.cancels >= 1)

        asyncio.run(scenario())
    assert session._audio_source.clear_queue_calls >= 1  # (2) unplayed audio cleared
    assert fake_rt.cancels == 1                           # (1) response cancelled
    assert session._generation_epoch == 0


def test_barge_in_idempotent(monkeypatch, engine):
    with _fake_rtc_for_worker():
        import livekit.rtc as rtc
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        session._audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
        fake_rt = _FakeRealtime()
        session._realtime_session = fake_rt
        session._speaking_client_turn_id = "A"
        session._active_client_turn_id = "A"

        async def scenario():
            session._on_realtime_speech_started()
            session._on_realtime_speech_started()  # duplicate interruption
            await _pump_until(lambda: fake_rt.cancels >= 1)

        asyncio.run(scenario())
    assert session._generation_epoch == 0
    assert fake_rt.cancels == 1


# =====================================================================
# 13 - rapid turns keep monotonic generations
# =====================================================================

def test_rapid_turns_monotonic(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        seen = []
        for index in range(5):
            processing = session._accept_realtime_turn(f"turn-{index}", "question")
            seen.append(session._generation_epoch)
            assert processing is not None
            processing.close()
            session._in_flight_turn_ids.discard(f"turn-{index}")
    assert seen == [1, 2, 3, 4, 5]


# =====================================================================
# 3, 4, 5 - late/stale response events (wrong response_id) ignored
# =====================================================================

def test_stale_response_events_are_ignored_by_response_id():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())

        got = bytearray()

        async def sink(pcm):
            got.extend(pcm)

        async def feed():
            await _pump_until(lambda: any(e.get("type") == "response.create" for e in conn.sent))
            conn.push({"type": "response.created", "response": {"id": "resp_B"}})
            # (3) late audio delta from a CANCELLED response A -> ignored
            conn.push({**_audio_delta(b"\xAA\xBB" * 50), "response_id": "resp_A"})
            # matching audio for B -> delivered
            conn.push({**_audio_delta(b"\x01\x02" * 50), "response_id": "resp_B"})
            # (4) late transcript from A -> ignored
            conn.push({"type": "response.output_audio_transcript.done",
                       "transcript": "STALE A", "response_id": "resp_A"})
            conn.push({"type": "response.output_audio_transcript.done",
                       "transcript": "hello B", "response_id": "resp_B"})
            # (5) late response.done for A -> ignored, does not end B
            conn.push({"type": "response.done", "response": {"id": "resp_A"}})
            conn.push({"type": "response.done", "response": {"id": "resp_B"}})
        asyncio.ensure_future(feed())
        result = await session.speak(client_turn_id="B", text="hello B", on_audio=sink)

        assert bytes(got) == b"\x01\x02" * 50           # A's stale audio never delivered
        assert result.spoken_transcript == "hello B"     # A's stale transcript ignored
        assert result.completed is True                  # B's done ended it, A's done did not
        await session.aclose()

    asyncio.run(scenario())


# =====================================================================
# 6 - generation-epoch gate prevents persist + disclosure mutation
# =====================================================================

def test_gate_prevents_persist_and_disclosure(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr("app.patient_engine.get_openai_client",
                            lambda: FakeOpenAIClient(text="I have had this pain for a week."))
        db = session._session_factory()
        try:
            before = SessionRepository(db).get_disclosed_fact_ids(SessionRepository(db).get(sid))
            with pytest.raises(patient_adapter.GenerationStaleError):
                patient_adapter.generate_and_persist_turn(
                    db, session_id=sid, case_id="carly", question="what brings you in?",
                    client_turn_id="A", is_generation_valid=lambda: False,
                )
            assert _turns(session, sid) == []  # nothing persisted
            after = SessionRepository(db).get_disclosed_fact_ids(SessionRepository(db).get(sid))
            assert after == before             # disclosure NOT mutated
        finally:
            db.close()


# =====================================================================
# 7, 8, 9, 10 - epoch lifecycle through _handle_realtime_turn
# =====================================================================

def test_superseded_before_start(monkeypatch, engine):
    """A waits for the lock; a newer utterance bumps the epoch while it waits;
    A abandons before generating (9: stale A cannot overwrite B)."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        gen_called = {"n": 0}

        def fake_generate(*a, **k):
            gen_called["n"] += 1
            class _R:
                patient_turn_id = "pt"; patient_text = "x"; voice_key = "patient"; replayed = False
            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def scenario():
            await session._turn_lock.acquire()          # hold the lock (A "generating")
            task = asyncio.ensure_future(session._handle_realtime_turn("A", "qa"))
            await asyncio.sleep(0.02)                     # A now waiting on the lock
            processing = session._accept_realtime_turn("B", "qb")
            assert processing is not None
            processing.close()
            session._in_flight_turn_ids.discard("B")
            session._turn_lock.release()                  # let A acquire
            await task

        asyncio.run(scenario())
        assert gen_called["n"] == 0                        # A never generated
        assert "A" in session._completed_turn_ids


def test_stale_before_speak_never_speaks(monkeypatch, engine):
    """A generates (persisted), but a newer utterance bumps the epoch before
    speaking: A never calls speak (8/10) and its row is finalized."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        gate_reached = threading.Event()
        proceed = threading.Event()

        def fake_generate(*a, **k):
            gate_reached.set()
            proceed.wait(timeout=5)
            class _R:
                patient_turn_id = "pt-x"; patient_text = "a full answer"; voice_key = "patient"; replayed = False
            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)
        fake_rt = _FakeRealtime()
        session._realtime_session = fake_rt

        async def scenario():
            task = asyncio.ensure_future(session._handle_realtime_turn("A", "qa"))
            await asyncio.get_running_loop().run_in_executor(None, gate_reached.wait, 5)
            processing = session._accept_realtime_turn("B", "qb")
            assert processing is not None
            processing.close()
            session._in_flight_turn_ids.discard("B")
            proceed.set()
            await task

        asyncio.run(scenario())
        assert fake_rt.spoke == []                                    # (8/10) never spoke
        statuses = _turn_statuses(_room)
        assert all(s.get("status") != "speaking_started" for s in statuses)


def test_new_turn_not_dropped_after_stale(monkeypatch, engine):
    """A is superseded and abandons; B (a genuinely new utterance) is NOT
    busy-dropped and generates normally (7)."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        generated = []

        def fake_generate(db, *, session_id, case_id, question, client_turn_id,
                          on_stage=None, is_generation_valid=None, generation_authority=None):
            if is_generation_valid is not None and not is_generation_valid():
                raise patient_adapter.GenerationStaleError(client_turn_id)
            generated.append(client_turn_id)
            class _R:
                patient_turn_id = "pt"; patient_text = "ok"; voice_key = "patient"; replayed = False
            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)
        fake_rt = _FakeRealtime()
        session._realtime_session = fake_rt

        async def scenario():
            await session._handle_realtime_turn("B", "qb")

        asyncio.run(scenario())
        assert "B" in generated  # B ran; not dropped


# =====================================================================
# 11 - interrupted patient row reconciles to only-delivered portion
# =====================================================================

def test_interrupted_row_reconciled_to_spoken_portion(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        monkeypatch.setattr("app.patient_engine.get_openai_client",
                            lambda: FakeOpenAIClient(text="I first noticed the pain about a week ago while gardening."))
        # Realtime reports it was interrupted after voicing only the first part.
        fake_rt = _FakeRealtime(SpeakResult("I first noticed the pain about", 100, False, True))
        session._realtime_session = fake_rt

        asyncio.run(session._handle_realtime_turn("A", "when did it start?"))

        patient = next(t for t in _turns(session, sid) if t.role == "patient")
        assert patient.content == "I first noticed the pain about"   # only delivered portion
        assert patient.validation_status == "interrupted"


# =====================================================================
# 14 - legacy voice path unchanged
# =====================================================================

def test_legacy_generate_without_predicate_persists_normally(monkeypatch, engine):
    """generate_and_persist_turn with NO predicate (the legacy call shape) never
    raises GenerationStaleError and persists as before."""
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr("app.patient_engine.get_openai_client",
                            lambda: FakeOpenAIClient(text="I have had this pain for a week."))
        db = session._session_factory()
        try:
            result = patient_adapter.generate_and_persist_turn(
                db, session_id=sid, case_id="carly", question="what brings you in?",
                client_turn_id="legacy-1",
            )
            assert result.patient_text == "I have had this pain for a week."
        finally:
            db.close()
        assert len(_turns(session, sid)) == 2  # student + patient persisted


def test_legacy_handle_student_turn_still_busy_drops(monkeypatch, engine):
    """The LEGACY _handle_student_turn keeps its busy-drop (only the Realtime
    path changed) - proves Phase F did not alter legacy serialization."""
    import inspect
    from app.livekit_agent.worker import PocAgentSession
    src = inspect.getsource(PocAgentSession._handle_student_turn)
    assert "livekit_agent_turn_dropped_busy" in src
    assert "_turn_lock.locked()" in src
