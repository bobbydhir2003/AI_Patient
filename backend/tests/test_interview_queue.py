"""Interview waiting-queue tests: real capacity gate, positions, dedup,
admission, and the critical guarantee that leaving the queue never touches
sessions/assessments/grading.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import AssessmentRun, InterviewSession, Student
from app.services import interview_queue as q
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token, register


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _reset_queue():
    q._reset_for_tests()
    yield
    q._reset_for_tests()


def _seed_active(db, n, case_id="camden"):
    """Create N active interview sessions (fills real capacity)."""
    for i in range(n):
        st = Student(name=f"Active {i}", student_number=f"A{i}", email=f"a{i}@x.edu")
        db.add(st)
        db.flush()
        db.add(InterviewSession(student_id=st.id, case_id=case_id, status="active",
                                started_at=datetime.now(timezone.utc)))
    db.commit()


def _small_limit(monkeypatch, n=1):
    monkeypatch.setattr(get_settings(), "max_concurrent_ai_interviews", n)


# ----------------------------------------------------------- capacity gate
def test_admitted_when_capacity_available(engine, monkeypatch):
    _small_limit(monkeypatch, 2)
    db = _factory(engine)()
    try:
        r = q.join(db, "stud1", "camden")
        assert r["admitted"] is True and r["position"] == 0
    finally:
        db.close()


def test_queued_when_full(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        _seed_active(db, 1)  # system full (1 active == limit 1)
        r = q.join(db, "stud1", "camden")
        assert r["admitted"] is False
        assert r["position"] == 1
        assert r["entry_id"]
        assert r["total_waiting"] == 1
    finally:
        db.close()


def test_dedup_same_student(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        _seed_active(db, 1)
        r1 = q.join(db, "stud1", "camden")
        r2 = q.join(db, "stud1", "camden")  # refresh / double click
        assert r1["entry_id"] == r2["entry_id"]
        assert r2["position"] == 1  # not 2
        assert r2["total_waiting"] == 1
    finally:
        db.close()


def test_positions_and_leave(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        _seed_active(db, 1)
        a = q.join(db, "studA", "camden")
        b = q.join(db, "studB", "camden")
        assert a["position"] == 1 and b["position"] == 2
        # A leaves -> B moves up to position 1.
        q.leave(db, a["entry_id"], "studA")
        assert q.status(db, b["entry_id"])["position"] == 1
    finally:
        db.close()


def test_automatic_admission_when_slot_frees(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        _seed_active(db, 1)
        a = q.join(db, "studA", "camden")
        assert a["admitted"] is False
        # Free the slot: mark the active session completed.
        s = db.query(InterviewSession).filter_by(status="active").first()
        s.status = "completed"
        db.commit()
        # Next poll admits the front waiter.
        r = q.status(db, a["entry_id"])
        assert r["admitted"] is True
        # And the entry is now gone (admitted once).
        assert q.status(db, a["entry_id"])["state"] == "expired"
    finally:
        db.close()


# ----------------------------------------------------------- SAFETY: no grading loss
def test_leave_does_not_touch_session_or_assessment(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        # A completed session with an assessment run (grading result) exists.
        st = Student(name="Grad", student_number="G1", email="g1@x.edu")
        db.add(st); db.flush()
        done = InterviewSession(student_id=st.id, case_id="camden", status="completed",
                                started_at=datetime.now(timezone.utc))
        db.add(done); db.flush()
        run = AssessmentRun(session_id=done.id, case_id="camden", status="completed")
        db.add(run); db.commit()
        run_id = run.id
        done_id = done.id

        _seed_active(db, 1)  # fill capacity so the SAME student must queue
        entry = q.join(db, st.id, "camden")
        assert entry["admitted"] is False
        q.leave(db, entry["entry_id"], st.id)

        # The completed session + assessment MUST remain intact after leaving.
        assert db.get(InterviewSession, done_id) is not None
        assert db.get(InterviewSession, done_id).status == "completed"
        assert db.get(AssessmentRun, run_id) is not None
        assert db.get(AssessmentRun, run_id).status == "completed"
    finally:
        db.close()


def test_estimated_wait_only_with_history(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    db = _factory(engine)()
    try:
        _seed_active(db, 1)
        r = q.join(db, "studA", "camden")
        # No completed interviews yet -> no fabricated wait time.
        assert r["estimated_wait_minutes"] is None
    finally:
        db.close()


# ----------------------------------------------------------- API auth
def test_queue_endpoints_require_auth(engine, monkeypatch):
    _small_limit(monkeypatch, 1)
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        assert c.post("/api/queue/join", json={"caseId": "camden"}).status_code == 401
        register(c, email="qstud@school.edu", password="studpass1", number="Q1")
        sh = bearer(login_token(c, "qstud@school.edu", "studpass1"))
        r = c.post("/api/queue/join", json={"caseId": "camden"}, headers=sh)
        assert r.status_code == 200
        assert "admitted" in r.json()
