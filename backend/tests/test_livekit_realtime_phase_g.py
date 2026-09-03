"""Phase G (Realtime transcript + frontend sync) - see worker.py's
_send_transcript_sync and the emit points in _run_realtime_turn.

The DB stays authoritative; these events only make the visible conversation
reflect the Realtime engine promptly and reconcile after interruption. No
network. Covers the required matrix:
 1 student Realtime transcript persists                (test_student_transcript_*)
 2 patient text available BEFORE speech completes      (test_patient_text_ready_before_*)
 3 patient_text_ready tied to correct clientTurnId     (test_patient_text_ready_correlation)
 4 stale patient text ignored                          (test_stale_turn_emits_no_patient_text)
 5 interrupted reconciles to DB partial                (test_interrupted_reconcile_event)
 6 no duplicate transcript rows                        (test_no_duplicate_rows)
 7 rapid turns ordered                                 (test_rapid_turns_ordered)
 8 legacy frontend path unchanged                      (test_legacy_path_emits_no_transcript_sync)
 9 assessment sees same ConversationTurn structure     (test_conversation_turn_structure_intact)
"""
import asyncio
import threading

from app.core.constants import ROLE_PATIENT, ROLE_STUDENT
from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_session import SpeakResult
from app.repositories.transcript_repository import TranscriptRepository
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_realtime_phase_ef import _FakeRealtime
from tests.test_livekit_phase_c import _make_ready_session, _turn_statuses
from tests.test_livekit_poc import _fake_rtc_for_worker


def _sync_events(room, event_type=None):
    out = []
    for topic, body, _dest in room.local_participant.published_data:
        if topic == "transcript_sync" and (event_type is None or body.get("type") == event_type):
            out.append(body)
    return out


def _ordered_labels(room):
    """Flat ordered stream of (topic-kind) labels across both topics, so we can
    assert relative ordering of a transcript_sync event vs a turn status."""
    labels = []
    for topic, body, _dest in room.local_participant.published_data:
        if topic == "transcript_sync":
            labels.append(body.get("type"))
        elif topic == "patient_turn_status":
            labels.append(f"status:{body.get('status')}")
    return labels


def _turns(session, sid):
    db = session._session_factory()
    try:
        return TranscriptRepository(db).list_turns(sid)
    finally:
        db.close()


def _wire_engine(session, monkeypatch, *, text="I have had this pain for a week.", speak_result=None):
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: FakeOpenAIClient(text=text))
    fake_rt = _FakeRealtime(speak_result)
    session._realtime_session = fake_rt
    return fake_rt


# =====================================================================
# 1, 3 - student transcript + patient_text_ready correlation
# =====================================================================

def test_student_transcript_emitted_and_persisted(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch)
        asyncio.run(session._handle_realtime_turn("realtime-g-1", "what brings you in today?"))

    st = _sync_events(room, "student_transcript")
    assert len(st) == 1
    assert st[0]["text"] == "what brings you in today?"
    assert st[0]["clientTurnId"] == "realtime-g-1"
    # Persisted student row matches.
    student = next(t for t in _turns(session, sid) if t.role == ROLE_STUDENT)
    assert student.content == "what brings you in today?"


def test_patient_text_ready_correlation(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch, text="I have had it two days.")
        asyncio.run(session._handle_realtime_turn("realtime-g-2", "how long?"))

    ready = _sync_events(room, "patient_text_ready")
    assert len(ready) == 1
    assert ready[0]["text"] == "I have had it two days."
    assert ready[0]["clientTurnId"] == "realtime-g-2"
    assert "patientTurnId" in ready[0] and ready[0]["patientTurnId"]
    assert "epoch" in ready[0]
    patient = next(t for t in _turns(session, sid) if t.role == ROLE_PATIENT)
    assert ready[0]["patientTurnId"] == patient.id  # ties to the real row


# =====================================================================
# 2 - patient text available BEFORE speech completion
# =====================================================================

def test_patient_text_ready_before_speaking_ended(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch)
        asyncio.run(session._handle_realtime_turn("realtime-g-3", "hello?"))

    labels = _ordered_labels(room)
    assert "patient_text_ready" in labels
    assert "status:speaking_ended" in labels
    assert labels.index("patient_text_ready") < labels.index("status:speaking_ended")
    # And it precedes speaking_started too (rendered as speech begins).
    assert labels.index("patient_text_ready") < labels.index("status:speaking_started")


# =====================================================================
# 4 - stale patient text ignored (withheld for a superseded turn)
# =====================================================================

def test_stale_turn_emits_no_patient_text(monkeypatch, engine):
    """A generates (student row persisted -> student_transcript emitted), but a
    newer utterance bumps the epoch before speaking: patient_text_ready is NEVER
    emitted for the stale turn."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        gate_reached = threading.Event()
        proceed = threading.Event()

        def fake_generate(*a, **k):
            gate_reached.set()
            proceed.wait(timeout=5)
            class _R:
                patient_turn_id = "pt-x"; patient_text = "stale answer"; voice_key = "patient"; replayed = False
            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)
        session._realtime_session = _FakeRealtime()

        async def scenario():
            task = asyncio.ensure_future(session._handle_realtime_turn("A", "qa"))
            await asyncio.get_running_loop().run_in_executor(None, gate_reached.wait, 5)
            session._active_client_turn_id = session._active_client_turn_id or "A"
            processing = session._accept_realtime_turn("B", "qb")
            assert processing is not None
            processing.close()
            session._in_flight_turn_ids.discard("B")
            proceed.set()
            await task

        asyncio.run(scenario())

    assert _sync_events(room, "student_transcript")          # student text shown (it was said)
    assert _sync_events(room, "patient_text_ready") == []     # stale patient text withheld
    assert _sync_events(room, "patient_text_final") == []


# =====================================================================
# 5 - interrupted reconciles to the delivered portion
# =====================================================================

def test_interrupted_reconcile_event(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(
            session, monkeypatch,
            text="I first noticed the pain about a week ago while gardening.",
            speak_result=SpeakResult("I first noticed the pain about", 100, False, True),
        )
        asyncio.run(session._handle_realtime_turn("realtime-g-5", "when did it start?"))

    final = _sync_events(room, "patient_text_final")
    assert len(final) == 1
    assert final[0]["text"] == "I first noticed the pain about"   # delivered portion only
    assert final[0]["reason"] == "interrupted"
    # Frontend reconcile matches the DB row.
    patient = next(t for t in _turns(session, sid) if t.role == ROLE_PATIENT)
    assert patient.content == final[0]["text"]


# =====================================================================
# 6, 9 - no duplicate rows; ConversationTurn structure intact
# =====================================================================

def test_no_duplicate_rows(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch)
        asyncio.run(session._handle_realtime_turn("realtime-g-6", "q1?"))
        # A duplicate of the SAME id must not create a second pair.
        asyncio.run(session._handle_realtime_turn("realtime-g-6", "q1?"))

    turns = _turns(session, sid)
    assert sum(1 for t in turns if t.role == ROLE_STUDENT) == 1
    assert sum(1 for t in turns if t.role == ROLE_PATIENT) == 1


def test_conversation_turn_structure_intact(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch)
        asyncio.run(session._handle_realtime_turn("realtime-g-9", "what brings you in?"))

    turns = _turns(session, sid)
    student = next(t for t in turns if t.role == ROLE_STUDENT)
    patient = next(t for t in turns if t.role == ROLE_PATIENT)
    # Same authoritative structure assessment/transcript readers rely on.
    assert student.turn_index == 0 and patient.turn_index == 1
    assert student.client_turn_id == "realtime-g-9"
    assert patient.speaker_id and patient.content


# =====================================================================
# 7 - rapid sequential turns stay ordered with monotonic epochs
# =====================================================================

def test_rapid_turns_ordered(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _wire_engine(session, monkeypatch)
        asyncio.run(session._handle_realtime_turn("realtime-g-7a", "first?"))
        asyncio.run(session._handle_realtime_turn("realtime-g-7b", "second?"))

    ready = _sync_events(room, "patient_text_ready")
    assert [r["clientTurnId"] for r in ready] == ["realtime-g-7a", "realtime-g-7b"]
    # student transcripts in order too
    st = [s["clientTurnId"] for s in _sync_events(room, "student_transcript")]
    assert st == ["realtime-g-7a", "realtime-g-7b"]


# =====================================================================
# 8 - legacy path emits NO transcript_sync
# =====================================================================

def test_legacy_path_emits_no_transcript_sync(monkeypatch, engine):
    """The legacy browser-text turn (_handle_student_turn/_run_turn) must never
    emit the new Realtime-only transcript_sync events."""
    from app.livekit_agent import patient_adapter as pa
    from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: FakeOpenAIClient(text="Legacy answer."))
        give_carly_a_voice_id(monkeypatch)
        monkeypatch.setattr(pa, "get_elevenlabs_client", lambda: FakeElevenLabsClient(chunks=(b"\x01\x02",)))

        async def _drive():
            await session._handle_student_turn("How long?", "legacy-g-1")

        asyncio.run(_drive())

    assert _sync_events(room) == []  # legacy path untouched by Phase G
