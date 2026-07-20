"""Admin panel: views, management actions, cascade deletion, audit log."""
from sqlalchemy.orm import sessionmaker

from app.models import (
    AssessmentDomainResult,
    AssessmentEvidence,
    AssessmentRun,
    AuditLog,
    ConversationTurn,
    InterviewSession,
    Student,
)
from tests.test_auth import auth_header, login_token, make_admin


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def admin_token(client, engine, email="admin@school.edu", password="adminpass1"):
    make_admin(engine, email, password)
    return login_token(client, email, password)


def seed_full_student(engine, name="Casey", number="C1"):
    """A student with one completed session, a transcript, and a full assessment
    (run -> domain -> evidence anchored to a turn). Returns ids for assertions."""
    db = _factory(engine)()
    try:
        student = Student(name=name, student_number=number, email=f"{number}@x.edu")
        db.add(student)
        db.flush()
        session = InterviewSession(
            student_id=student.id, case_id="carly", status="completed", locked=True
        )
        db.add(session)
        db.flush()
        t0 = ConversationTurn(session_id=session.id, turn_index=0, role="student", content="How are you?")
        t1 = ConversationTurn(session_id=session.id, turn_index=1, role="patient", content="Sore.")
        db.add_all([t0, t1])
        db.flush()
        run = AssessmentRun(session_id=session.id, case_id="carly", status="COMPLETE", overall_level="Developing")
        db.add(run)
        db.flush()
        domain = AssessmentDomainResult(
            assessment_run_id=run.id, rubric_domain="oars_communication", performance_level="Developing"
        )
        db.add(domain)
        db.flush()
        ev = AssessmentEvidence(
            domain_result_id=domain.id, turn_id=t0.id, evidence_type="strength", label="Good opener"
        )
        db.add(ev)
        db.commit()
        return {
            "student_id": student.id,
            "session_id": session.id,
            "run_id": run.id,
            "turn_id": t0.id,
        }
    finally:
        db.close()


def counts(engine):
    db = _factory(engine)()
    try:
        return {
            "students": db.query(Student).count(),
            "sessions": db.query(InterviewSession).count(),
            "turns": db.query(ConversationTurn).count(),
            "runs": db.query(AssessmentRun).count(),
            "domains": db.query(AssessmentDomainResult).count(),
            "evidence": db.query(AssessmentEvidence).count(),
        }
    finally:
        db.close()


# --------------------------------------------------------------------- views
def test_admin_dashboard(client, engine):
    seed_full_student(engine)
    token = admin_token(client, engine)
    r = client.get("/api/admin/dashboard", headers=auth_header(token))
    assert r.status_code == 200
    body = r.json()
    assert body["totalStudents"] == 1
    assert body["totalSessions"] == 1
    assert body["completedSessions"] == 1
    assert body["totalAssessments"] == 1


def test_admin_can_view_students_with_search_and_filter(client, engine):
    seed_full_student(engine, name="Alice", number="A9")
    seed_full_student(engine, name="Bob", number="B9")
    token = admin_token(client, engine)
    all_r = client.get("/api/admin/students", headers=auth_header(token))
    assert all_r.json()["total"] == 2
    # search by name
    s = client.get("/api/admin/students?search=alice", headers=auth_header(token))
    assert s.json()["total"] == 1 and s.json()["items"][0]["name"] == "Alice"


def test_admin_can_view_transcript_and_assessment(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    tr = client.get(f"/api/admin/sessions/{ids['session_id']}/transcript", headers=auth_header(token))
    assert tr.status_code == 200
    assert [m["speaker"] for m in tr.json()] == ["student", "patient"]
    a = client.get(f"/api/admin/sessions/{ids['session_id']}/assessment", headers=auth_header(token))
    assert a.status_code == 200
    assert a.json()["overallLevel"] == "Developing"


def test_admin_student_detail_and_sessions(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    d = client.get(f"/api/admin/students/{ids['student_id']}", headers=auth_header(token))
    assert d.json()["sessionCount"] == 1 and d.json()["completedCount"] == 1
    sess = client.get(f"/api/admin/students/{ids['student_id']}/sessions", headers=auth_header(token))
    assert sess.json()[0]["hasAssessment"] is True


# --------------------------------------------------------------------- archive
def test_admin_can_archive_and_reactivate_student(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    r = client.patch(
        f"/api/admin/students/{ids['student_id']}/status",
        headers=auth_header(token),
        json={"isActive": False},
    )
    assert r.status_code == 200
    d = client.get(f"/api/admin/students/{ids['student_id']}", headers=auth_header(token))
    assert d.json()["isActive"] is False
    # reactivate
    client.patch(
        f"/api/admin/students/{ids['student_id']}/status",
        headers=auth_header(token),
        json={"isActive": True},
    )
    d2 = client.get(f"/api/admin/students/{ids['student_id']}", headers=auth_header(token))
    assert d2.json()["isActive"] is True


def test_admin_can_archive_session(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    r = client.patch(f"/api/admin/sessions/{ids['session_id']}/archive", headers=auth_header(token))
    assert r.status_code == 200
    got = client.get(f"/api/admin/sessions/{ids['session_id']}", headers=auth_header(token))
    assert got.json()["status"] == "archived"


# --------------------------------------------------------------------- delete
def test_admin_can_delete_session_cascades_turns_and_assessment(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    before = counts(engine)
    r = client.request("DELETE", f"/api/admin/sessions/{ids['session_id']}", headers=auth_header(token))
    assert r.status_code == 200
    after = counts(engine)
    assert after["sessions"] == before["sessions"] - 1
    assert after["turns"] == 0
    assert after["runs"] == 0 and after["domains"] == 0 and after["evidence"] == 0
    assert after["students"] == before["students"]  # student profile kept


def test_admin_can_delete_assessment_only(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    r = client.request("DELETE", f"/api/admin/assessments/{ids['run_id']}", headers=auth_header(token))
    assert r.status_code == 200
    after = counts(engine)
    assert after["runs"] == 0 and after["domains"] == 0 and after["evidence"] == 0
    assert after["sessions"] == 1 and after["turns"] == 2  # session + transcript intact


def test_permanent_student_delete_requires_confirmation(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    bad = client.request(
        "DELETE", f"/api/admin/students/{ids['student_id']}",
        headers=auth_header(token), json={"confirm": "nope"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "delete_confirmation_required"
    # data untouched
    assert counts(engine)["students"] == 1


def test_permanent_student_delete_cascades_everything(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    r = client.request(
        "DELETE", f"/api/admin/students/{ids['student_id']}",
        headers=auth_header(token), json={"confirm": "DELETE"},
    )
    assert r.status_code == 200
    after = counts(engine)
    assert after == {"students": 0, "sessions": 0, "turns": 0, "runs": 0, "domains": 0, "evidence": 0}


def test_admin_cannot_delete_own_account(client, engine):
    # Create an admin that is ALSO linked to a student profile, then try to
    # delete that student -> blocked.
    db = _factory(engine)()
    try:
        from app.core.security import hash_password
        from app.models import User
        student = Student(name="Self", student_number="SELF", email="self@x.edu")
        db.add(student)
        db.flush()
        admin = User(
            email="self-admin@school.edu", password_hash=hash_password("adminpass1"),
            role="admin", is_active=True, student_id=student.id,
        )
        db.add(admin)
        db.commit()
        student_id = student.id
    finally:
        db.close()
    token = login_token(client, "self-admin@school.edu", "adminpass1")
    r = client.request(
        "DELETE", f"/api/admin/students/{student_id}",
        headers=auth_header(token), json={"confirm": "DELETE"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "self_deletion_forbidden"


# --------------------------------------------------------------------- audit
def test_destructive_actions_write_audit_log(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    client.patch(f"/api/admin/sessions/{ids['session_id']}/archive", headers=auth_header(token))
    client.request("DELETE", f"/api/admin/assessments/{ids['run_id']}", headers=auth_header(token))

    db = _factory(engine)()
    try:
        actions = {a.action_type for a in db.query(AuditLog).all()}
    finally:
        db.close()
    assert "session_archived" in actions
    assert "assessment_deleted" in actions

    log = client.get("/api/admin/audit-logs", headers=auth_header(token))
    assert log.status_code == 200
    assert log.json()["total"] >= 2
    assert log.json()["items"][0]["adminEmail"] == "admin@school.edu"


def test_delete_message_removes_evidence(client, engine):
    ids = seed_full_student(engine)
    token = admin_token(client, engine)
    # The seeded evidence is anchored to turn_id; deleting the message must not
    # leave orphaned evidence.
    r = client.request("DELETE", f"/api/admin/messages/{ids['turn_id']}", headers=auth_header(token))
    assert r.status_code == 200
    after = counts(engine)
    assert after["turns"] == 1  # the patient turn remains
    assert after["evidence"] == 0  # evidence for the deleted message is gone
