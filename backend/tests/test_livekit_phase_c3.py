"""Phase C3: unique LiveKit room per intentional voice connection.

Fixes the confirmed restart race: room names were previously deterministic
(`ptai-interview-{session_id}`), so a Stop-then-Start (or refresh, or leave/
return) could reconnect to a room that might still be shutting down in
LiveKit Cloud - since RoomAgentDispatch only re-applies at room CREATION,
joining a still-existing room could silently skip a fresh worker dispatch,
leaving the student stuck waiting for an agent_ready that would never come.

Every call to create_student_token now mints a fresh, server-generated
connection_id (UUID4) baked into the room name
(student_room_name(session_id, connection_id)) - the browser never supplies
or influences it, and the WORKER's interview identity (session_id/case_id)
is carried entirely by RoomAgentDispatch metadata, never by parsing the room
name - so this change requires zero worker.py logic changes (see
test_worker_metadata_parsing_is_unaffected_by_room_name_suffix below).

The admin POC path (create_poc_token/poc_room_name) is deliberately
UNCHANGED - its room stays deterministic, connection_id is always "" there.
"""
import json

import jwt

from app.core.config import get_settings
from app.livekit_agent.worker import parse_job_metadata
from app.services import livekit_token_service
from tests.test_livekit_phase_a import LIVEKIT_API_SECRET, LIVEKIT_URL, _enable_livekit_for_students, _student_client
from tests.test_voice import seed_owned_session


def _owned_session_id(client, case_id="carly") -> str:
    resp = client.post("/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": case_id})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["sessionId"]


# --------------------------------------------------------------- A/B: uniqueness + format

def test_same_session_two_token_requests_get_different_connection_ids_and_rooms(engine, monkeypatch):
    """TEST A: same interview session, two separate token requests (e.g. the
    Stop-then-Start-again flow) - same session_id, two DIFFERENT
    connectionIds, two DIFFERENT roomNames."""
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    first = client.post(f"/api/interviews/{session_id}/livekit-token").json()
    second = client.post(f"/api/interviews/{session_id}/livekit-token").json()

    assert first["connectionId"] != second["connectionId"]
    assert first["roomName"] != second["roomName"]
    # Same underlying interview both times - proven by both room names
    # sharing the exact same session_id segment.
    assert first["roomName"].startswith(f"ptai-interview-{session_id}-")
    assert second["roomName"].startswith(f"ptai-interview-{session_id}-")


def test_room_name_format_contains_session_prefix_and_unique_connection_suffix(engine, monkeypatch):
    """TEST B: room name format - session prefix for log correlation, unique
    connection identifier for room uniqueness."""
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    body = client.post(f"/api/interviews/{session_id}/livekit-token").json()
    room_name = body["roomName"]
    connection_id = body["connectionId"]

    assert room_name.startswith("ptai-interview-")
    assert session_id in room_name, "session_id must remain visible in the room name for log correlation"
    assert connection_id in room_name
    assert room_name == f"ptai-interview-{session_id}-{connection_id}"
    # UUID4 shape: 36 chars, 4 hyphens.
    assert len(connection_id) == 36
    assert connection_id.count("-") == 4


# --------------------------------------------------------------- C: browser cannot choose room/connection

def test_browser_cannot_supply_or_influence_room_name_or_connection_id(engine, monkeypatch):
    """TEST C: the endpoint takes no request body at all (session_id comes
    from the URL path, already ownership-verified) - an attacker-supplied
    body is simply irrelevant, proven here by sending one anyway and
    confirming the server-generated values are used regardless."""
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    r = client.post(
        f"/api/interviews/{session_id}/livekit-token",
        json={"roomName": "attacker-chosen-room", "connectionId": "attacker-chosen-id"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "attacker-chosen-room" not in body["roomName"]
    assert body["connectionId"] != "attacker-chosen-id"
    assert body["roomName"].startswith(f"ptai-interview-{session_id}-")


# --------------------------------------------------------------- G: interview session unaffected

def test_multiple_voice_connections_never_create_a_new_interview_session(engine, monkeypatch, db_session):
    """TEST G: minting several LiveKit tokens (simulating Start -> Stop ->
    Start -> Stop -> Start) for the SAME session must never create a second
    InterviewSession row - session_id, case_id, and every other session
    field are untouched by voice-connection churn."""
    from app.models import InterviewSession

    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client, case_id="carly")

    count_before = db_session.query(InterviewSession).count()
    for _ in range(3):
        r = client.post(f"/api/interviews/{session_id}/livekit-token")
        assert r.status_code == 200, r.text
    count_after = db_session.query(InterviewSession).count()

    assert count_after == count_before, "minting LiveKit tokens must never create a new InterviewSession"
    session = db_session.get(InterviewSession, session_id)
    assert session is not None
    assert session.case_id == "carly"


# --------------------------------------------------------------- K: worker metadata parsing unaffected

def test_worker_metadata_parsing_is_unaffected_by_room_name_suffix(engine, monkeypatch):
    """TEST K: the worker learns session_id/case_id EXCLUSIVELY from
    RoomAgentDispatch job metadata (parse_job_metadata), never from the room
    name - proven here by decoding a REAL minted token (with its new
    connection_id-suffixed room name) and feeding its dispatch metadata
    through the actual worker.py parser."""
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client, case_id="carly")

    body = client.post(f"/api/interviews/{session_id}/livekit-token").json()
    decoded = jwt.decode(
        body["token"], LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    metadata_raw = decoded["roomConfig"]["agents"][0]["metadata"]

    parsed = parse_job_metadata(metadata_raw)
    assert parsed == (session_id, "carly")
    # The room name itself is NOT valid job metadata JSON - confirms the
    # worker could never accidentally fall back to parsing it.
    assert parse_job_metadata(body["roomName"]) is None


# --------------------------------------------------------------- L: cross-worker idempotency

def test_same_client_turn_id_across_two_simulated_workers_only_generates_once(engine, monkeypatch):
    """TEST L: Worker #1 (old room, still finishing an in-flight turn) and
    Worker #2 (new room, dispatched moments later) are two SEPARATE
    PocAgentSession-equivalent call paths, each with their own DB session -
    but generate_and_persist_turn's client_turn_id uniqueness is enforced at
    the DATABASE level, not in either worker's in-memory dedup state, so a
    duplicate submission across the two is still safe: only one OpenAI
    generation, one persisted turn."""
    from sqlalchemy.orm import sessionmaker

    from app.livekit_agent import patient_adapter
    from tests.conftest import FakeOpenAIClient

    fake_openai = FakeOpenAIClient(text="Same answer every time.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db_worker_1 = factory()
    _user, session_id = seed_owned_session(db_worker_1, case_id="carly")

    # Worker #1 (old room) processes the turn first.
    first = patient_adapter.generate_and_persist_turn(
        db_worker_1, session_id=session_id, case_id="carly",
        question="Where does it hurt?", client_turn_id="shared-turn-1",
    )
    db_worker_1.close()

    # Worker #2 (new room, brand-new DB session - simulating a totally
    # separate process) receives a duplicate/retried submission of the
    # SAME clientTurnId (e.g. the browser's own delivery-ack retry firing
    # right as the voice connection handed over).
    db_worker_2 = factory()
    second = patient_adapter.generate_and_persist_turn(
        db_worker_2, session_id=session_id, case_id="carly",
        question="Where does it hurt?", client_turn_id="shared-turn-1",
    )
    db_worker_2.close()

    assert len(fake_openai.calls) == 1, "OpenAI must be called exactly once across both workers"
    assert first.replayed is False
    assert second.replayed is True
    assert second.patient_turn_id == first.patient_turn_id
    assert second.patient_text == first.patient_text


# --------------------------------------------------------------- Q: assessment sees full transcript

def test_transcript_spans_multiple_simulated_voice_connections_in_order(engine, monkeypatch):
    """TEST Q (transcript half): turns generated by "Worker #1" (before a
    Stop) and "Worker #2" (after a Start again) land in ONE continuous,
    correctly-ordered transcript for the session - proving context
    continuity is purely DB-backed, never dependent on any one worker
    process staying alive (see patient_adapter.generate_and_persist_turn's
    use of transcript_repo.list_turns(session_id), never a cached history)."""
    from sqlalchemy.orm import sessionmaker

    from app.livekit_agent import patient_adapter
    from app.repositories.transcript_repository import TranscriptRepository
    from tests.conftest import FakeOpenAIClient

    fake_openai = FakeOpenAIClient(text="An answer.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db1 = factory()
    _user, session_id = seed_owned_session(db1, case_id="carly")

    # "Worker #1" (voice connection #1) handles 2 turns, then the student
    # Stops (worker #1's process/DB session goes away).
    for i in range(2):
        patient_adapter.generate_and_persist_turn(
            db1, session_id=session_id, case_id="carly",
            question=f"Question {i}", client_turn_id=f"conn1-turn-{i}",
        )
    db1.close()

    # "Worker #2" (voice connection #2, a brand-new DB session/process)
    # continues the SAME session after Start-again.
    db2 = factory()
    for i in range(2):
        patient_adapter.generate_and_persist_turn(
            db2, session_id=session_id, case_id="carly",
            question=f"Question {i + 2}", client_turn_id=f"conn2-turn-{i}",
        )

    turns = TranscriptRepository(db2).list_turns(session_id)
    db2.close()

    # 4 student turns + 4 patient turns = 8, in strict chronological order.
    assert len(turns) == 8
    assert [t.turn_index for t in turns] == list(range(8))
    student_questions = [t.content for t in turns if t.role == "student"]
    assert student_questions == ["Question 0", "Question 1", "Question 2", "Question 3"]
