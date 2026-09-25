"""Admin Student Data: list, per-student detail (sessions, assessment viewing,
visit sources, survey state, timeline) and the admin survey reset.

REDCap I/O is intercepted at the redcap_client boundary (no network)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    AssessmentRun,
    AssessmentViewSession,
    AssessmentViewVisit,
    AuditLog,
    ConversationTurn,
    InterviewSession,
    Student,
    SurveyReceipt,
    SurveyReceiptReset,
    User,
)
from app.core.constants import USER_ROLE_ADMIN
from app.schemas.session_schema import SessionCreateRequest
from app.services import assessment_view_service as avs
from app.services import session_service, student_data_service
from tests.conftest import make_client
from tests.test_admin import admin_token, super_admin_token
from tests.test_auth import auth_header
from tests.test_surveys import CARLY, POST_ANSWERS, PRE_ANSWERS, SOFIA, _new_session

T0 = datetime(2026, 3, 1, 15, 0, 0, tzinfo=timezone.utc)


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def api(engine, fake_client):
    """One client: no header = the seeded default STUDENT; bearer = real auth."""
    with make_client(engine, fake_client, authenticate=True) as c:
        yield c


@pytest.fixture()
def admin(api, engine):
    return auth_header(admin_token(api, engine))


@pytest.fixture()
def superadmin(api, engine):
    """Survey resets are system administration (super_admin only)."""
    return auth_header(super_admin_token(api, engine))


@pytest.fixture()
def redcap(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.redcap_client.import_record", lambda fields: calls.append(dict(fields))
    )
    return calls


def _default_student_id(engine) -> str:
    db = _factory(engine)()
    try:
        return db.execute(select(Student.id).where(Student.student_number == "D1")).scalar_one()
    finally:
        db.close()


def _seed_student(engine, *, name, number, email=None, practice=False, active=True):
    db = _factory(engine)()
    try:
        s = Student(
            name=name, student_number=number, email=email or f"{number}@x.edu",
            is_practice=practice, is_active=active,
        )
        db.add(s)
        db.flush()
        user = User(
            email=email or f"{number}@x.edu", password_hash="x", full_name=name,
            student_number=number, role="student", student_id=s.id, is_active=True,
        )
        db.add(user)
        db.commit()
        return s.id, user.id
    finally:
        db.close()


def _seed_session(
    engine, student_id, *, case="camden", status="completed", questions=2,
    level="Proficient", minutes=12, started=T0, with_run=True,
):
    db = _factory(engine)()
    try:
        s = InterviewSession(
            student_id=student_id, case_id=case, status=status,
            locked=status == "completed", started_at=started,
            completed_at=started + timedelta(minutes=minutes) if status == "completed" else None,
        )
        db.add(s)
        db.flush()
        idx = 0
        for _ in range(questions):
            db.add(ConversationTurn(session_id=s.id, turn_index=idx, role="student", content="Q?"))
            db.add(ConversationTurn(session_id=s.id, turn_index=idx + 1, role="patient", content="A."))
            idx += 2
        run_id = None
        if with_run:
            run = AssessmentRun(
                session_id=s.id, case_id=case, status="COMPLETE", overall_level=level,
                completed_at=started + timedelta(minutes=minutes + 1),
            )
            db.add(run)
            db.flush()
            run_id = run.id
        db.commit()
        return s.id, run_id
    finally:
        db.close()


def _ping(engine, run_id, user_id, visit_id, source, at):
    db = _factory(engine)()
    try:
        run = db.get(AssessmentRun, run_id)
        user = db.get(User, user_id)
        avs.record_ping(db, run=run, user=user, visit_id=visit_id, source=source, now=at)
    finally:
        db.close()


def _count(engine, model, *where):
    db = _factory(engine)()
    try:
        stmt = select(model)
        for w in where:
            stmt = stmt.where(w)
        return len(db.execute(stmt).scalars().all())
    finally:
        db.close()


# =========================================================================== list
def test_list_requires_admin(api, engine, fake_client):
    # Default (student) identity.
    assert api.get("/api/admin/student-data").status_code == 403
    with make_client(engine, fake_client, authenticate=False) as anon:
        assert anon.get("/api/admin/student-data").status_code == 401


def test_list_search_pagination_and_practice_excluded(api, admin, engine):
    _seed_student(engine, name="Alice Adams", number="11111111", email="alice@unmc.edu")
    _seed_student(engine, name="Bob Brown", number="22222222")
    _seed_student(engine, name="Practice Pat", number="99999999", practice=True)
    sid, _ = _seed_student(engine, name="Cara Cole", number="33333333", active=False)
    _seed_session(engine, sid, status="active", with_run=False)

    r = api.get("/api/admin/student-data?page_size=100", headers=admin)
    assert r.status_code == 200
    names = [i["name"] for i in r.json()["items"]]
    assert "Practice Pat" not in names
    assert names == sorted(names, key=str.lower)  # default sort = name

    cara = next(i for i in r.json()["items"] if i["name"] == "Cara Cole")
    assert cara["isActive"] is False
    assert cara["sessionCount"] == 1 and cara["incompleteCount"] == 1
    assert cara["lastActivityAt"] is not None

    for q in ("alice", "11111111", "alice@unmc"):
        hit = api.get(f"/api/admin/student-data?search={q}", headers=admin).json()
        assert [i["name"] for i in hit["items"]] == ["Alice Adams"], q

    p1 = api.get("/api/admin/student-data?page=1&page_size=2", headers=admin).json()
    p2 = api.get("/api/admin/student-data?page=2&page_size=2", headers=admin).json()
    assert p1["total"] == p2["total"] >= 4
    assert not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}

    inactive = api.get("/api/admin/student-data?status=inactive", headers=admin).json()
    assert [i["name"] for i in inactive["items"]] == ["Cara Cole"]


# ========================================================================= detail
def test_detail_returns_only_this_students_sessions_with_metrics(api, admin, engine):
    a_id, a_user = _seed_student(engine, name="Ann", number="A1")
    b_id, _ = _seed_student(engine, name="Ben", number="B1")
    s1, r1 = _seed_session(engine, a_id, case="camden", questions=3, minutes=10)
    s2, _ = _seed_session(
        engine, a_id, case="carly", status="active", with_run=False,
        started=T0 + timedelta(days=1),
    )
    _seed_session(engine, b_id, case="sofia")

    done = T0 + timedelta(minutes=20)
    _ping(engine, r1, a_user, "v1", "initial_assessment", done)
    _ping(engine, r1, a_user, "v1", "initial_assessment", done + timedelta(seconds=20))
    _ping(engine, r1, a_user, "v2", "student_dashboard", done + timedelta(hours=1))
    _ping(engine, r1, a_user, "v2", "student_dashboard", done + timedelta(hours=1, seconds=15))

    r = api.get(f"/api/admin/student-data/{a_id}", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["student"]["name"] == "Ann"
    assert [s["sessionId"] for s in body["sessions"]] == [s2, s1]  # newest first

    row = next(s for s in body["sessions"] if s["sessionId"] == s1)
    assert row["studentQuestionCount"] == 3
    assert row["durationSeconds"] == 600
    assert row["overallLevel"] == "Proficient" and row["assessmentId"] == r1
    assert row["activeViewingSeconds"] == 35 and row["viewCount"] == 2
    assert [(v["visitNumber"], v["source"], v["activeSeconds"]) for v in row["visits"]] == [
        (1, "initial_assessment", 20),
        (2, "student_dashboard", 15),
    ]
    open_row = next(s for s in body["sessions"] if s["sessionId"] == s2)
    assert open_row["hasAssessment"] is False and open_row["visits"] == []
    assert open_row["activeViewingSeconds"] is None

    sm = body["summary"]
    assert (sm["totalSessions"], sm["completedSessions"], sm["incompleteSessions"]) == (2, 1, 1)
    assert sm["totalInterviewSeconds"] == 600 and sm["averageInterviewSeconds"] == 600
    assert (sm["totalAssessments"], sm["totalVisits"], sm["totalViewSeconds"]) == (1, 2, 35)

    kinds = [e["kind"] for e in body["timeline"]]
    assert kinds.count("assessment_viewed") == 2
    assert "interview_completed" in kinds and "assessment_generated" in kinds
    assert any("After Interview Report" in e["detail"] for e in body["timeline"])
    assert any("Student Dashboard" in e["detail"] for e in body["timeline"])
    ats = [e["at"] for e in body["timeline"]]
    assert ats == sorted(ats, reverse=True)


def test_detail_zero_session_student(api, admin, engine):
    sid, _ = _seed_student(engine, name="Zed", number="Z1")
    body = api.get(f"/api/admin/student-data/{sid}", headers=admin).json()
    assert body["sessions"] == [] and body["timeline"] == []
    assert body["summary"]["totalSessions"] == 0
    assert body["summary"]["averageInterviewSeconds"] is None
    sv = body["survey"]
    assert sv["globalStatus"] == "not_started"
    assert not (sv["canResetPre"] or sv["canResetPost"] or sv["canResetBoth"])


def test_detail_unknown_student_404_and_student_role_denied(api, admin, engine):
    assert api.get("/api/admin/student-data/nope", headers=admin).status_code == 404
    sid, _ = _seed_student(engine, name="Yan", number="Y1")
    assert api.get(f"/api/admin/student-data/{sid}").status_code == 403


def test_visit_history_survives_deleted_assessment_and_legacy_source(api, admin, engine):
    sid, uid = _seed_student(engine, name="Lee", number="L1")
    s1, r1 = _seed_session(engine, sid)
    _ping(engine, r1, uid, "v1", "initial_assessment", T0 + timedelta(hours=1))
    db = _factory(engine)()
    try:
        db.add(AssessmentViewVisit(
            visit_id="legacy", interview_session_id=s1, assessment_run_id=None,
            user_id=uid, student_id=sid, case_id="camden", source="legacy",
            started_at=T0 + timedelta(minutes=30), last_heartbeat_at=T0 + timedelta(minutes=31),
            active_seconds=50,
        ))
        # Run deleted later (FK SET NULL in Postgres; emulate on SQLite).
        for v in db.execute(select(AssessmentViewVisit)).scalars():
            v.assessment_run_id = None
        db.delete(db.get(AssessmentRun, r1))
        db.commit()
    finally:
        db.close()

    row = api.get(f"/api/admin/student-data/{sid}", headers=admin).json()["sessions"][0]
    assert row["hasAssessment"] is False
    assert [(v["visitNumber"], v["source"]) for v in row["visits"]] == [
        (1, "legacy"), (2, "initial_assessment"),
    ]
    assert all(v["assessmentDeleted"] for v in row["visits"])


def test_detail_query_count_does_not_grow_with_sessions(api, admin, engine):
    """No N+1: a student with 6 sessions costs the same number of SQL
    statements as a student with 1."""
    one, u1 = _seed_student(engine, name="One", number="N1")
    many, u6 = _seed_student(engine, name="Many", number="N6")
    for sid, uid, n in ((one, u1, 1), (many, u6, 6)):
        for i in range(n):
            _s, run = _seed_session(engine, sid, started=T0 + timedelta(days=i))
            _ping(engine, run, uid, f"v{i}", "student_dashboard", T0 + timedelta(days=i, hours=1))

    statements: list[str] = []

    def _count_stmt(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count_stmt)
    try:
        statements.clear()
        assert api.get(f"/api/admin/student-data/{one}", headers=admin).status_code == 200
        n_one = len(statements)
        statements.clear()
        assert api.get(f"/api/admin/student-data/{many}", headers=admin).status_code == 200
        n_many = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", _count_stmt)
    assert n_one == n_many and n_one > 5, (n_one, n_many)


# =================================================================== survey reset
def _complete_survey(api, case=CARLY):
    sid = _new_session(api, case)
    assert api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    assert api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS).status_code == 200
    return sid


def _status(api, sid):
    return api.get(f"/api/interviews/{sid}/surveys/status").json()


def _reset(api, admin, student_id, scope, **extra):
    return api.post(
        f"/api/admin/student-data/{student_id}/survey-reset",
        json={"scope": scope, **extra}, headers=admin,
    )


def test_reset_post_only(api, superadmin, engine, redcap):
    sid = _complete_survey(api)
    student_id = _default_student_id(engine)
    [old] = [r for r in _receipts_for(engine, student_id)]
    old_record = old.redcap_record_id

    r = _reset(api, superadmin, student_id, "post")
    assert r.status_code == 200, r.text
    sv = r.json()["survey"]
    assert sv["pre"]["completed"] is True and sv["post"]["completed"] is False
    assert sv["globalStatus"] == "in_progress" and sv["ownerCaseId"] == CARLY
    assert sv["resetCount"] == 1

    [rec] = _receipts_for(engine, student_id)
    assert rec.redcap_record_id != old_record  # retake goes to a NEW REDCap record
    [snap] = _resets_for(engine, student_id)
    assert (snap.scope, snap.redcap_record_id, snap.post_sync_status) == ("post", old_record, "synced")
    assert snap.post_synced_at is not None

    # Student sees Post again for the owner case and can submit it.
    st = _status(api, sid)
    assert st["postSubmitted"] is False and st["globalSurveyStatus"] == "in_progress"
    redcap.clear()
    assert api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS).status_code == 200
    assert [c["record_id"] for c in redcap] == [rec.redcap_record_id]
    assert _status(api, sid)["globalSurveyStatus"] == "completed"


def test_reset_pre_only(api, superadmin, engine, redcap):
    sid = _complete_survey(api)
    student_id = _default_student_id(engine)
    r = _reset(api, superadmin, student_id, "pre")
    assert r.status_code == 200, r.text
    sv = r.json()["survey"]
    assert sv["pre"]["completed"] is False and sv["post"]["completed"] is True
    st = _status(api, sid)
    assert st["preSubmitted"] is False and st["isSurveyOwnerCase"] is True
    redcap.clear()
    assert api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    assert len(redcap) == 1
    # Pre redone + Post still done -> package completed again.
    assert _status(api, sid)["globalSurveyStatus"] == "completed"


def test_reset_both_makes_repeated_case_student_eligible_again(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    old_record = _receipts_for(engine, student_id)[0].redcap_record_id
    sofia = _new_session(api, SOFIA)
    assert _status(api, sofia)["isSurveyOwnerCase"] is False  # would show "skip"

    r = _reset(api, superadmin, student_id, "both")
    assert r.status_code == 200, r.text
    assert r.json()["survey"]["globalStatus"] == "not_started"
    assert _receipts_for(engine, student_id) == []
    [snap] = _resets_for(engine, student_id)
    assert snap.redcap_record_id == old_record and snap.was_owner_case is True

    st = _status(api, sofia)
    assert st["globalSurveyStatus"] == "not_started"
    redcap.clear()
    assert api.post(f"/api/interviews/{sofia}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    assert redcap[0]["record_id"] != old_record


def test_reset_targets_only_that_student_and_keeps_all_other_data(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    s_id, run_id = _seed_session(engine, student_id, case="camden")
    uid = _factory(engine)().execute(
        select(User.id).where(User.student_id == student_id)
    ).scalar_one()
    _ping(engine, run_id, uid, "v1", "initial_assessment", T0 + timedelta(hours=1))

    other_id, _ = _seed_student(engine, name="Other", number="O1")
    db = _factory(engine)()
    try:
        other = db.get(Student, other_id)
        other.survey_owner_case_id = CARLY
        other.survey_completed_at = T0
        db.add(SurveyReceipt(
            student_id=other_id, case_id=CARLY, redcap_record_id="other-record",
            pre_sync_status="synced", post_sync_status="synced", overall_status="completed",
        ))
        db.commit()
    finally:
        db.close()

    before = {
        m.__name__: _count(engine, m)
        for m in (InterviewSession, ConversationTurn, AssessmentRun, AssessmentViewSession, AssessmentViewVisit)
    }
    # A studentId in the body is ignored: identity comes from the route only.
    assert _reset(api, superadmin, student_id, "both", studentId=other_id).status_code == 200
    after = {
        m.__name__: _count(engine, m)
        for m in (InterviewSession, ConversationTurn, AssessmentRun, AssessmentViewSession, AssessmentViewVisit)
    }
    assert before == after

    [other_receipt] = _receipts_for(engine, other_id)
    assert other_receipt.overall_status == "completed"
    db = _factory(engine)()
    try:
        other = db.get(Student, other_id)
        assert other.survey_owner_case_id == CARLY and other.survey_completed_at is not None
        audit = db.execute(select(AuditLog).where(AuditLog.action_type == "survey_reset")).scalars().all()
        assert len(audit) == 1 and audit[0].record_id == student_id
    finally:
        db.close()
    assert _resets_for(engine, other_id) == []


def test_reset_not_applicable_and_validation(api, superadmin, engine, redcap):
    student_id = _default_student_id(engine)
    assert _reset(api, superadmin, student_id, "both").status_code == 409  # nothing to reset
    sid = _new_session(api, CARLY)
    api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert _reset(api, superadmin, student_id, "post").status_code == 409  # Post never done
    assert _resets_for(engine, student_id) == []
    assert _reset(api, superadmin, student_id, "everything").status_code == 422
    assert _reset(api, superadmin, "missing-student", "both").status_code == 404


def test_reset_is_super_admin_only(api, admin, engine, redcap):
    _complete_survey(api)
    student_id = _default_student_id(engine)
    # No header = the seeded default STUDENT.
    r = api.post(f"/api/admin/student-data/{student_id}/survey-reset", json={"scope": "both"})
    assert r.status_code == 403
    # A normal admin can VIEW Student Data but cannot reset any scope.
    assert api.get(f"/api/admin/student-data/{student_id}", headers=admin).status_code == 200
    for scope in ("pre", "post", "both"):
        assert _reset(api, admin, student_id, scope).status_code == 403, scope
    assert _resets_for(engine, student_id) == []


def test_deleting_student_also_removes_reset_history(api, admin, superadmin, engine, redcap):
    sid, _ = _seed_student(engine, name="Del", number="DEL1")
    db = _factory(engine)()
    try:
        db.get(Student, sid).survey_owner_case_id = CARLY
        db.add(SurveyReceipt(
            student_id=sid, case_id=CARLY, redcap_record_id="rec",
            pre_sync_status="synced", post_sync_status="pending", overall_status="in_progress",
        ))
        db.commit()
    finally:
        db.close()
    assert _reset(api, superadmin, sid, "both").status_code == 200
    r = api.request(
        "DELETE", f"/api/admin/students/{sid}", json={"confirm": "DELETE"}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert _resets_for(engine, sid) == []


# --------------------------------------------------------------------- helpers
def _receipts_for(engine, student_id):
    db = _factory(engine)()
    try:
        return list(
            db.execute(select(SurveyReceipt).where(SurveyReceipt.student_id == student_id)).scalars()
        )
    finally:
        db.close()


def _resets_for(engine, student_id):
    db = _factory(engine)()
    try:
        return list(
            db.execute(
                select(SurveyReceiptReset).where(SurveyReceiptReset.student_id == student_id)
            ).scalars()
        )
    finally:
        db.close()


# ---------------------------------------------------- practice-session ownership
# Session.is_practice must follow the OWNING PROFILE, not the account's current
# role, so a real roster student promoted to admin never loses their sessions.
def test_promoted_student_admin_keeps_real_session(db_session):
    student = Student(name="Ella Hagen", student_number="E1", email="ella@x.edu", is_practice=False)
    db_session.add(student)
    db_session.flush()
    user = User(
        email="ella@x.edu", password_hash="x", full_name="Ella Hagen",
        student_number="E1", role=USER_ROLE_ADMIN, student_id=student.id, is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    payload = SessionCreateRequest(student_name="Ella Hagen", student_id="E1", case_id="camden")
    resp = session_service.create_session(db_session, payload, user)

    sess = db_session.get(InterviewSession, resp.session_id)
    assert sess.is_practice is False  # real student's session stays real
    assert sess.student_id == student.id

    # ...and it is therefore visible in that real student's Student Data.
    detail = student_data_service.get_student_data(db_session, student.id)
    assert detail.summary.total_sessions == 1
    assert any(r.session_id == resp.session_id for r in detail.sessions)


def test_pure_admin_gets_practice_session_excluded_from_student_data(db_session):
    admin = User(
        email="admin@x.edu", password_hash="x", full_name="Administrator",
        student_number="", role=USER_ROLE_ADMIN, student_id=None, is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    payload = SessionCreateRequest(student_name="Administrator", student_id="", case_id="camden")
    resp = session_service.create_session(db_session, payload, admin)

    sess = db_session.get(InterviewSession, resp.session_id)
    assert sess.is_practice is True  # admin-only practice profile => practice session
    prof = db_session.get(Student, sess.student_id)
    assert prof.is_practice is True  # provisioned as a practice profile
    # The practice profile is excluded from the real Student Data roster.
    assert prof.id not in [i.id for i in student_data_service.list_students(db_session).items]
