"""P0-3 candidate-audio, generation authority, and persistence races.

All tests are deterministic and local. Raw VAD boundaries are deliberately
separated from the accepted-transcription reservation seam.
"""
import asyncio
import threading

from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_session import RealtimeSession, SpeakResult
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_phase_c import _make_ready_session
from tests.test_livekit_poc import _fake_rtc_for_worker
from tests.test_livekit_realtime_phase_a import _pump_until
from tests.test_livekit_realtime_phase_d import _QueueConn, _audio_delta, _mk_session
from tests.test_livekit_realtime_phase_g import _sync_events


class _Result:
    def __init__(self, turn_id: str, text: str = "approved answer"):
        self.patient_turn_id = f"patient-{turn_id}"
        self.patient_text = text
        self.voice_key = "patient"
        self.replayed = False


class _Realtime:
    def __init__(self):
        self.spoke = []
        self.cancels = 0

    async def speak(self, *, client_turn_id, text, on_audio):
        self.spoke.append(client_turn_id)
        return SpeakResult(text, 2, True, False)

    async def cancel_active_response(self):
        self.cancels += 1


def _close_reserved(session, client_turn_id: str, text: str = "question") -> int:
    processing = session._accept_realtime_turn(client_turn_id, text)
    assert processing is not None
    generation = session._generation_epoch
    processing.close()
    session._in_flight_turn_ids.discard(client_turn_id)
    return generation


def test_transient_candidate_during_generation_does_not_stale_answer(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        entered = threading.Event()
        release = threading.Event()

        def generate(*args, is_generation_valid=None, **kwargs):
            entered.set()
            release.wait(timeout=5)
            assert is_generation_valid is not None and is_generation_valid()
            return _Result("A")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", generate)
        realtime = _Realtime()
        session._realtime_session = realtime

        async def scenario():
            task = asyncio.create_task(session._handle_realtime_turn("A", "question A"))
            assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            session._on_realtime_speech_started()
            assert session._generation_epoch == 1
            session._on_realtime_speech_stopped()
            release.set()
            await task

        asyncio.run(scenario())

    assert realtime.spoke == ["A"]
    assert session._generation_epoch == 1


def test_uncommitted_and_empty_audio_never_advance_generation(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        session._on_realtime_speech_started()
        session._on_realtime_speech_started()
        session._on_realtime_speech_stopped()
        assert session._accept_realtime_turn("empty", "   ") is None

    assert session._generation_epoch == 0
    assert session._realtime_student_speech_active is False
    assert session._realtime_student_speech_stopped.is_set()


def test_accepted_b_supersedes_a_with_no_stale_effects_or_output(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        entered = threading.Event()
        release = threading.Event()
        effects = {"rows": [], "disclosure": [], "topic": []}

        def generate(*args, client_turn_id, is_generation_valid=None, **kwargs):
            if client_turn_id == "A":
                entered.set()
                release.wait(timeout=5)
            if is_generation_valid is not None and not is_generation_valid():
                raise patient_adapter.GenerationStaleError(client_turn_id)
            effects["rows"].append(client_turn_id)
            effects["disclosure"].append(client_turn_id)
            effects["topic"].append(client_turn_id)
            return _Result(client_turn_id)

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", generate)
        realtime = _Realtime()
        session._realtime_session = realtime

        async def scenario():
            a = asyncio.create_task(session._handle_realtime_turn("A", "question A"))
            assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            b_work = session._accept_realtime_turn("B", "question B")
            assert b_work is not None
            assert session._generation_epoch == 2
            b = asyncio.create_task(b_work)
            release.set()
            await asyncio.gather(a, b)

        asyncio.run(scenario())

    assert effects == {"rows": ["B"], "disclosure": ["B"], "topic": ["B"]}
    assert realtime.spoke == ["B"]
    assert [e["clientTurnId"] for e in _sync_events(room, "patient_text_ready")] == ["B"]


def test_rapid_reservations_are_consecutive_and_only_c_can_run(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        generated = []

        def generate(*args, client_turn_id, **kwargs):
            generated.append(client_turn_id)
            return _Result(client_turn_id)

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", generate)
        session._realtime_session = _Realtime()

        async def scenario():
            await session._turn_lock.acquire()
            works = [session._accept_realtime_turn(cid, cid) for cid in ("A", "B", "C")]
            assert all(work is not None for work in works)
            assert session._generation_epoch == 3
            tasks = [asyncio.create_task(work) for work in works if work is not None]
            session._turn_lock.release()
            await asyncio.gather(*tasks)

        asyncio.run(scenario())

    assert generated == ["C"]
    assert "A" in session._completed_turn_ids and "B" in session._completed_turn_ids


def test_response_ready_waits_for_speech_stop_then_speaks(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr(
            patient_adapter, "generate_and_persist_turn",
            lambda *args, **kwargs: _Result("A"),
        )
        realtime = _Realtime()
        session._realtime_session = realtime

        async def scenario():
            session._on_realtime_speech_started()
            task = asyncio.create_task(session._handle_realtime_turn("A", "question A"))
            assert await _pump_until(lambda: bool(_sync_events(room, "student_transcript")))
            assert realtime.spoke == []
            assert _sync_events(room, "patient_text_ready") == []
            session._on_realtime_speech_stopped()
            await task

        asyncio.run(scenario())

    assert realtime.spoke == ["A"]
    assert len(_sync_events(room, "patient_text_ready")) == 1


def test_superseded_while_waiting_never_speaks(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr(
            patient_adapter, "generate_and_persist_turn",
            lambda *args, **kwargs: _Result(kwargs["client_turn_id"]),
        )
        realtime = _Realtime()
        session._realtime_session = realtime

        async def scenario():
            session._on_realtime_speech_started()
            task = asyncio.create_task(session._handle_realtime_turn("A", "question A"))
            assert await _pump_until(lambda: bool(_sync_events(room, "student_transcript")))
            assert _close_reserved(session, "B", "question B") == 2
            session._on_realtime_speech_stopped()
            await task

        asyncio.run(scenario())

    assert realtime.spoke == []
    assert _sync_events(room, "patient_text_ready") == []


def test_acceptance_closes_generating_to_speaking_cutoff_race(monkeypatch, engine):
    with _fake_rtc_for_worker():
        import livekit.rtc as rtc

        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        session._audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
        realtime = _Realtime()
        session._realtime_session = realtime
        session._on_realtime_speech_started()  # A was generating: no cutoff yet.
        session._speaking_client_turn_id = "A"  # A crosses boundary before B is accepted.

        async def scenario():
            assert _close_reserved(session, "B", "question B") == 1
            await _pump_until(lambda: realtime.cancels == 1)

        asyncio.run(scenario())

    assert realtime.cancels == 1
    assert session._audio_source.clear_queue_calls == 1


def test_teardown_clears_candidate_and_releases_waiter(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        session._on_realtime_speech_started()
        session._stop_all_student_audio_ingest(reason="test_teardown")

    assert session._realtime_student_speech_active is False
    assert session._realtime_student_speech_stopped.is_set()


def test_cancel_before_response_created_cannot_leak_late_pcm():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())
        received = bytearray()

        async def sink(pcm):
            received.extend(pcm)

        speaking = asyncio.create_task(
            session.speak(client_turn_id="A", text="answer A", on_audio=sink)
        )
        assert await _pump_until(lambda: any(e.get("type") == "response.create" for e in conn.sent))
        await session.cancel_active_response()
        first_result = await speaking

        second = asyncio.create_task(
            session.speak(client_turn_id="B", text="answer B", on_audio=sink)
        )
        await asyncio.sleep(0.02)
        assert len([e for e in conn.sent if e.get("type") == "response.create"]) == 1

        conn.push({"type": "response.created", "response": {"id": "late-A"}})
        conn.push({**_audio_delta(b"\x01\x02" * 20), "response_id": "late-A"})
        conn.push({"type": "response.done", "response": {"id": "late-A"}})
        assert await _pump_until(
            lambda: len([e for e in conn.sent if e.get("type") == "response.create"]) == 2
        )
        conn.push({"type": "response.created", "response": {"id": "response-B"}})
        conn.push({**_audio_delta(b"\x03\x04" * 20), "response_id": "response-B"})
        conn.push({"type": "response.done", "response": {"id": "response-B"}})
        second_result = await second

        assert first_result.interrupted is True
        assert second_result.completed is True
        assert received == b"\x03\x04" * 20
        assert [e for e in conn.sent if e.get("type") == "response.cancel"] == [
            {"type": "response.cancel", "response_id": "late-A"}
        ]
        await session.aclose()

    asyncio.run(scenario())


def test_generation_authority_lock_has_exactly_two_valid_orderings(monkeypatch, engine):
    """B-first makes A perform zero writes; A-first commits before B reserves."""
    with _fake_rtc_for_worker():
        # Ordering 1: B reserves while A is still generating.
        session, _room, sid = _make_ready_session(engine, monkeypatch)
        generation = {"value": 1}
        generation_lock = threading.Lock()
        generation_entered = threading.Event()
        generation_release = threading.Event()

        class BlockingOpenAI(FakeOpenAIClient):
            def generate(self, messages, usage_out=None):
                generation_entered.set()
                generation_release.wait(timeout=5)
                return super().generate(messages, usage_out)

        monkeypatch.setattr(
            "app.patient_engine.get_openai_client",
            lambda: BlockingOpenAI(text="I have had pain for a week."),
        )
        db = session._session_factory()
        outcome = []

        def run_a():
            try:
                patient_adapter.generate_and_persist_turn(
                    db, session_id=sid, case_id="carly", question="question A",
                    client_turn_id="A", is_generation_valid=lambda: generation["value"] == 1,
                    generation_authority=generation_lock,
                )
                outcome.append("committed")
            except patient_adapter.GenerationStaleError:
                outcome.append("stale")

        thread = threading.Thread(target=run_a)
        thread.start()
        assert generation_entered.wait(timeout=5)
        with generation_lock:
            generation["value"] = 2
        generation_release.set()
        thread.join(timeout=5)
        assert outcome == ["stale"]
        assert TranscriptRepository(db).list_turns(sid) == []
        seeded = SessionRepository(db).get(sid)
        assert SessionRepository(db).get_disclosed_fact_ids(seeded) == set()
        assert seeded.active_topic is None
        db.close()

        # Ordering 2: A reaches its short transaction first. B cannot reserve
        # until A's commit returns and the authority lock is released.
        session2, _room2, sid2 = _make_ready_session(engine, monkeypatch)
        monkeypatch.setattr(
            "app.patient_engine.get_openai_client",
            lambda: FakeOpenAIClient(text="I have had pain for a week."),
        )
        db2 = session2._session_factory()
        original_commit = db2.commit
        commit_entered = threading.Event()
        commit_release = threading.Event()
        reservation_done = threading.Event()
        generation2 = {"value": 1}
        lock2 = threading.Lock()

        def blocking_commit():
            commit_entered.set()
            commit_release.wait(timeout=5)
            original_commit()

        db2.commit = blocking_commit
        committed = []

        def commit_a():
            patient_adapter.generate_and_persist_turn(
                db2, session_id=sid2, case_id="carly", question="question A",
                client_turn_id="A2", is_generation_valid=lambda: generation2["value"] == 1,
                generation_authority=lock2,
            )
            committed.append("A2")

        def reserve_b():
            with lock2:
                generation2["value"] = 2
            reservation_done.set()

        a_thread = threading.Thread(target=commit_a)
        a_thread.start()
        assert commit_entered.wait(timeout=5)
        b_thread = threading.Thread(target=reserve_b)
        b_thread.start()
        assert not reservation_done.wait(timeout=0.05)
        commit_release.set()
        a_thread.join(timeout=5)
        b_thread.join(timeout=5)
        assert committed == ["A2"]
        assert reservation_done.is_set() and generation2["value"] == 2
        assert len(TranscriptRepository(db2).list_turns(sid2)) == 2
        db2.close()
