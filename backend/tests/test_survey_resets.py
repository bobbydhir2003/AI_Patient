"""Admin Survey Resets page: real-student survey state list + bulk reset.

Single-student resets reuse /admin/student-data/{id}/survey-reset (covered in
test_student_data.py); here they are exercised through the list to prove the
page's counts and rows follow them. REDCap I/O is intercepted (no network)."""
from sqlalchemy import select

from app.models import (
    AssessmentRun,
    AuditLog,
    ConversationTurn,
    InterviewSession,
    Student,
    SurveyReceipt,
)
from tests.conftest import make_client
from tests.test_student_data import (  # noqa: F401 - fixtures
    _count,
    _default_student_id,
    _factory,
    _receipts_for,
    _reset,
    _resets_for,
    _seed_session,
    _seed_student,
    admin,
    api,
    redcap,
)
from tests.test_surveys import CARLY

URL = "/api/admin/survey-resets"


def _seed_survey(engine, student_id, *, case=CARLY, pre="synced", post="synced", record=None):
    """A survey package in the given stage state, owned by ``case``."""
    db = _factory(engine)()
    try:
        s = db.get(Student, student_id)
        s.survey_owner_case_id = case
        done = pre == "synced" and post == "synced"
        if done:
            from datetime import datetime, timezone

            s.survey_completed_at = datetime.now(timezone.utc)
        db.add(SurveyReceipt(
            student_id=student_id, case_id=case, redcap_record_id=record or f"rec-{student_id}",
            pre_sync_status=pre, post_sync_status=post,
            overall_status="completed" if done else "in_progress",
        ))
        db.commit()
    finally:
        db.close()


def _roster(engine):
    """Three real students (both done / pre only / nothing) + one practice
    profile that has a completed survey and must never be listed or reset."""
    both, _ = _seed_student(engine, name="Ava Both", number="A1", email="ava@unmc.edu")
    pre_only, _ = _seed_student(engine, name="Ben Pre", number="B2")
    none_, _ = _seed_student(engine, name="Cal None", number="C3")
    practice, _ = _seed_student(engine, name="Pat Practice", number="P9", practice=True)
    _seed_survey(engine, both)
    _seed_survey(engine, pre_only, post="pending")
    _seed_survey(engine, practice)
    return both, pre_only, none_, practice


def _list(api, admin, **params):
    r = api.get(URL, params={"page_size": 100, **params}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def _row(data, student_id):
    return next(i for i in data["items"] if i["studentId"] == student_id)


# ============================================================================ list
def test_list_rows_statuses_and_summary(api, admin, engine):
    both, pre_only, none_, practice = _roster(engine)
    data = _list(api, admin)

    ids = [i["studentId"] for i in data["items"]]
    assert practice not in ids  # practice profile excluded
    assert {both, pre_only, none_} <= set(ids)

    a = _row(data, both)
    assert (a["preStatus"], a["postStatus"]) == ("completed", "completed")
    assert a["surveyCaseId"] == CARLY and a["email"] == "ava@unmc.edu"
    assert a["canResetPre"] and a["canResetPost"] and a["canResetBoth"]
    b = _row(data, pre_only)
    assert (b["preStatus"], b["postStatus"]) == ("completed", "pending")
    assert b["canResetPre"] and not b["canResetPost"] and b["canResetBoth"]
    c = _row(data, none_)
    assert (c["preStatus"], c["postStatus"]) == ("not_started", "not_started")
    assert not (c["canResetPre"] or c["canResetPost"] or c["canResetBoth"])

    s = data["summary"]
    # Seeded default student (D1) is real with no survey -> counted too.
    assert s["totalStudents"] == data["total"] == len(ids)
    assert (s["preCompleted"], s["postCompleted"]) == (2, 1)
    assert s["availableForReset"] == 2
    assert (s["preResettable"], s["postResettable"], s["bothResettable"]) == (2, 1, 2)
    assert data["caseOptions"] == [{"id": CARLY, "name": a["surveyCaseName"]}]


def test_list_search_filters_and_pagination(api, admin, engine):
    both, pre_only, none_, _ = _roster(engine)
    for q in ("ava", "A1", "ava@unmc"):
        assert [i["studentId"] for i in _list(api, admin, search=q)["items"]] == [both], q
    assert [i["studentId"] for i in _list(api, admin, status="completed")["items"]] == [both]
    assert [i["studentId"] for i in _list(api, admin, status="in_progress")["items"]] == [pre_only]
    not_started = [i["studentId"] for i in _list(api, admin, status="not_started")["items"]]
    assert none_ in not_started and both not in not_started
    assert {i["studentId"] for i in _list(api, admin, case_id=CARLY)["items"]} == {both, pre_only}
    assert none_ in [i["studentId"] for i in _list(api, admin, case_id="none")["items"]]

    p1 = _list(api, admin, page=1, page_size=2)
    p2 = _list(api, admin, page=2, page_size=2)
    assert p1["total"] == p2["total"] >= 4 and len(p1["items"]) == 2
    assert not {i["studentId"] for i in p1["items"]} & {i["studentId"] for i in p2["items"]}
    # Summary is roster-wide: independent of search/filters/page.
    assert _list(api, admin, search="ava")["summary"] == p1["summary"]
    assert api.get(URL, params={"status": "bogus"}, headers=admin).status_code == 422


def test_list_last_activity_uses_student_data_definition(api, admin, engine):
    sid, _ = _seed_student(engine, name="Dee Active", number="D9")
    _seed_session(engine, sid)
    row = _row(_list(api, admin, search="D9"), sid)
    detail = api.get(f"/api/admin/student-data/{sid}", headers=admin).json()
    assert row["lastActivityAt"] == detail["student"]["lastActivityAt"] is not None


# ================================================== single-student resets (reused API)
def test_single_resets_update_row_and_counts(api, admin, engine, redcap):
    both, pre_only, _, _ = _roster(engine)
    assert _reset(api, admin, both, "pre").status_code == 200
    row = _row(_list(api, admin), both)
    assert (row["preStatus"], row["postStatus"]) == ("pending", "completed")
    assert row["resetCount"] == 1 and row["lastResetAt"] is not None

    assert _reset(api, admin, both, "post").status_code == 200
    row = _row(_list(api, admin), both)
    assert (row["preStatus"], row["postStatus"]) == ("pending", "pending")

    assert _reset(api, admin, pre_only, "both").status_code == 200
    data = _list(api, admin)
    row = _row(data, pre_only)
    assert (row["preStatus"], row["postStatus"]) == ("not_started", "not_started")
    assert data["summary"]["preCompleted"] == 0 and data["summary"]["postCompleted"] == 0
    assert data["summary"]["availableForReset"] == 1  # `both` still has a package
    # History preserved: one snapshot per reset, earlier record ids kept.
    assert [s.redcap_record_id for s in _resets_for(engine, pre_only)] == [f"rec-{pre_only}"]
    assert len(_resets_for(engine, both)) == 2
    assert _list(api, admin, status="reset_before")["total"] == 2


# ============================================================================ bulk
def _bulk(api, admin, scope, **extra):
    return api.post(f"{URL}/bulk", json={"scope": scope, **extra}, headers=admin)


def test_bulk_pre(api, admin, engine, redcap):
    both, pre_only, none_, practice = _roster(engine)
    old = {sid: _receipts_for(engine, sid)[0].redcap_record_id for sid in (both, pre_only)}
    r = _bulk(api, admin, "pre")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["reset"], body["failed"]) == (2, 0)
    assert body["eligible"] == body["reset"] + body["skipped"]
    for sid in (both, pre_only):
        [rec] = _receipts_for(engine, sid)
        assert rec.pre_sync_status == "pending" and rec.redcap_record_id != old[sid]
        [snap] = _resets_for(engine, sid)
        assert (snap.scope, snap.redcap_record_id) == ("pre", old[sid])
    # Post of the fully-completed student untouched.
    assert _receipts_for(engine, both)[0].post_sync_status == "synced"
    # Practice profile never touched.
    assert _receipts_for(engine, practice)[0].pre_sync_status == "synced"
    assert _resets_for(engine, practice) == []
    assert _resets_for(engine, none_) == []


def test_bulk_post(api, admin, engine, redcap):
    both, pre_only, _, practice = _roster(engine)
    body = _bulk(api, admin, "post").json()
    assert (body["reset"], body["failed"]) == (1, 0)  # only `both` had Post done
    [rec] = _receipts_for(engine, both)
    assert (rec.pre_sync_status, rec.post_sync_status) == ("synced", "pending")
    assert _resets_for(engine, pre_only) == []
    assert _receipts_for(engine, practice)[0].post_sync_status == "synced"
    s = _list(api, admin)["summary"]
    assert (s["preCompleted"], s["postCompleted"], s["postResettable"]) == (2, 0, 0)


def test_bulk_both_preserves_history_and_other_data(api, admin, engine, redcap):
    both, pre_only, _, practice = _roster(engine)
    _seed_session(engine, both)
    models = (InterviewSession, ConversationTurn, AssessmentRun)
    before = {m.__name__: _count(engine, m) for m in models}

    body = _bulk(api, admin, "both").json()
    assert (body["reset"], body["failed"]) == (2, 0)
    assert "2 students" in body["message"]
    for sid in (both, pre_only):
        assert _receipts_for(engine, sid) == []
        [snap] = _resets_for(engine, sid)
        assert snap.redcap_record_id == f"rec-{sid}" and snap.scope == "both"
    db = _factory(engine)()
    try:
        assert db.get(Student, both).survey_owner_case_id is None
        assert db.get(Student, practice).survey_owner_case_id == CARLY
        audit = db.execute(select(AuditLog).where(AuditLog.action_type == "survey_reset")).scalars().all()
        # One row per student + one bulk summary row.
        assert sorted(a.record_type for a in audit) == ["student", "student", "survey_reset_bulk"]
    finally:
        db.close()
    assert before == {m.__name__: _count(engine, m) for m in models}
    assert _list(api, admin)["summary"]["availableForReset"] == 0

    # Nothing left: a repeat run resets no one and fails no one.
    again = _bulk(api, admin, "both").json()
    assert (again["reset"], again["failed"]) == (0, 0)


def test_bulk_ignores_body_student_ids(api, admin, engine, redcap):
    both, _, _, practice = _roster(engine)
    body = _bulk(api, admin, "post", studentIds=[practice], studentId=practice).json()
    assert body["reset"] == 1
    assert _resets_for(engine, practice) == [] and len(_resets_for(engine, both)) == 1


def test_bulk_one_failure_does_not_block_others(api, admin, engine, redcap, monkeypatch):
    both, pre_only, _, _ = _roster(engine)
    from app.services import student_data_service as sds

    real = sds.apply_survey_reset

    def flaky(db, admin_user, student_id, *, scope):
        if student_id == both:
            raise RuntimeError("boom")
        return real(db, admin_user, student_id, scope=scope)

    monkeypatch.setattr(sds, "apply_survey_reset", flaky)
    body = _bulk(api, admin, "pre").json()
    assert (body["reset"], body["failed"]) == (1, 1)
    assert _receipts_for(engine, both)[0].pre_sync_status == "synced"  # unchanged
    assert _resets_for(engine, both) == []
    assert _receipts_for(engine, pre_only)[0].pre_sync_status == "pending"


# ======================================================================= security
def test_invalid_scope_rejected(api, admin, engine, redcap):
    both, *_ = _roster(engine)
    for bad in ("everything", "", None):
        assert _bulk(api, admin, bad).status_code == 422
    assert api.post(f"{URL}/bulk", json={}, headers=admin).status_code == 422
    assert _resets_for(engine, both) == []


def test_student_and_anonymous_rejected(api, engine, fake_client, redcap):
    both, *_ = _roster(engine)
    # No header = the seeded default STUDENT identity.
    assert api.get(URL).status_code == 403
    assert api.post(f"{URL}/bulk", json={"scope": "both"}).status_code == 403
    assert api.post(
        f"/api/admin/student-data/{both}/survey-reset", json={"scope": "both"}
    ).status_code == 403
    with make_client(engine, fake_client, authenticate=False) as anon:
        assert anon.get(URL).status_code == 401
        assert anon.post(f"{URL}/bulk", json={"scope": "both"}).status_code == 401
    assert _resets_for(engine, both) == []
    assert _default_student_id(engine)  # default student still exists / untouched
