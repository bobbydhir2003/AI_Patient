"""Case-level Pre/Post experience survey tests.

Business rule under test: ONE survey package per (student, case). Pre and Post
are the two STAGES of that single package (UNIQUE(student_id, case_id)) - not
per-session, not per-phase. A new interview session for the same case, a
double-click, or a concurrent request can never create a second package, and a
completed package can never be reopened.

Also covers (retained from the original phase-level suite): mandatory NUID at
registration, exact REDCap field mapping, record_id == NUID (never PII), Likert
validation + unknown-field rejection, REDCap failure/retry, ownership isolation,
and the token never appearing in responses.

REDCap network I/O is intercepted at the redcap_client boundary so no real
REDCap call is ever made.
"""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.conftest import auth_headers, make_client

CARLY = "carly"
CAMDEN = "camden"
SOFIA = "sofia"
JAYDEN = "jayden"
ALL_CASES = [CARLY, CAMDEN, SOFIA, JAYDEN]

PRE_ANSWERS = {
    "pre_conf_begin": 4,
    "pre_conf_questions": 5,
    "pre_conf_unexpected": 3,
    "pre_conf_interview": 4,
    "pre_helpful_draft": 5,
}

POST_ANSWERS = {
    "post_conf_begin": 4,
    "post_conf_questions": 5,
    "post_conf_unexpected": 3,
    "post_conf_interview": 4,
    "post_helpful_draft": 5,
    "post_realistic": 4,
    "post_consistent": 4,
    "post_strengths_weaknesses": 5,
    "post_safe_mistakes": 5,
    "post_feedback_accurate": 3,
    "post_feedback_actionable": 4,
    "post_use_again": 5,
    "post_oe_most_helpful": "  Practicing open-ended questions.  ",
    "post_oe_unrealistic": "Nothing major.",
    "post_oe_one_change": "More cases.",
    "post_oe_feedback_type": "Specific examples.",
    "post_oe_feedback_missing": "",
}


@pytest.fixture()
def captured(monkeypatch):
    """Intercept redcap_client so REDCap is treated as configured and every
    import is captured (no network). Returns the list of field dicts."""
    calls: list[dict] = []
    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.redcap_client.import_record", lambda fields: calls.append(dict(fields))
    )
    return calls


@pytest.fixture()
def failing_redcap(monkeypatch):
    """REDCap configured but every import raises (timeout/HTTP/transport)."""
    from app.services.redcap_client import RedcapError

    def _boom(_fields):
        raise RedcapError("REDCap request failed.")

    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", _boom)


@pytest.fixture()
def student_api(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        yield c


def _new_session(client, case_id=CARLY, headers=None):
    r = client.post(
        "/api/sessions",
        json={"studentName": "S", "studentId": "", "caseId": case_id},
        headers=headers or {},
    )
    assert r.status_code == 201, r.text
    return r.json()["sessionId"]


def _set_default_student_number(engine, number: str):
    from app.models import Student

    db = sessionmaker(bind=engine)()
    try:
        s = db.query(Student).first()
        s.student_number = number
        db.commit()
    finally:
        db.close()


def _receipts(engine, case_id=None):
    from app.models import SurveyReceipt

    db = sessionmaker(bind=engine)()
    try:
        q = db.query(SurveyReceipt)
        if case_id is not None:
            q = q.filter(SurveyReceipt.case_id == case_id)
        return q.all()
    finally:
        db.close()


# ==========================================================================
# Retained coverage: registration NUID, mapping, validation, isolation, token
# ==========================================================================
@pytest.mark.parametrize("number", ["", "   "])
def test_registration_requires_nonblank_nuid(engine, number):
    with make_client(engine, authenticate=False) as c:
        r = c.post(
            "/api/auth/register",
            json={"fullName": "No Nuid", "email": "nonuid@school.edu",
                  "password": "password1", "studentNumber": number},
        )
        assert r.status_code == 422, r.text


def test_pre_survey_maps_all_fields_record_id_and_case_event(student_api, captured):
    sid = _new_session(student_api, CAMDEN)
    r = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert r.status_code == 200, r.text
    assert r.json()["syncStatus"] == "synced"
    assert r.json()["overallStatus"] == "in_progress"

    assert len(captured) == 1
    fields = captured[0]
    assert fields["record_id"] == "D1"  # seeded default student's NUID, server-resolved
    # Exactly: record_id + per-case event routing + the 5 pre vars + completion.
    assert set(fields) == set(PRE_ANSWERS) | {
        "record_id", "redcap_event_name", "pre_experience_survey_complete"
    }
    # Per-case instance routing keeps Camden separate from the other cases.
    assert fields["redcap_event_name"] == "camden_arm_1"
    assert fields["pre_experience_survey_complete"] == 2


def test_no_name_or_email_sent_to_redcap(student_api, captured):
    sid = _new_session(student_api)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    fields = captured[0]
    for pii_key in ("name", "email", "full_name", "student_name", "studentName"):
        assert pii_key not in fields
    values = {str(v) for v in fields.values()}
    assert "Default Student" not in values
    assert "default@school.edu" not in values


def test_post_survey_maps_all_fields(student_api, captured):
    sid = _new_session(student_api)
    # Pre must complete first (Pre + Interview + Post = one package).
    assert student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    r = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert r.status_code == 200, r.text
    fields = captured[1]  # captured[0] = Pre, captured[1] = Post
    assert fields["record_id"] == "D1"
    assert set(fields) == set(POST_ANSWERS) | {
        "record_id", "redcap_event_name", "post_experience_survey_complete"
    }
    assert fields["redcap_event_name"] == "carly_arm_1"
    assert fields["post_experience_survey_complete"] == 2
    assert fields["post_use_again"] == 5
    assert fields["post_oe_most_helpful"] == "Practicing open-ended questions."  # sanitised
    assert fields["post_oe_feedback_missing"] == ""


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
def test_likert_out_of_range_rejected(student_api, captured, bad):
    sid = _new_session(student_api)
    r = student_api.post(f"/api/interviews/{sid}/surveys/pre", json={**PRE_ANSWERS, "pre_conf_begin": bad})
    assert r.status_code == 422, r.text
    assert captured == []


def test_missing_likert_field_rejected(student_api, captured):
    sid = _new_session(student_api)
    payload = dict(PRE_ANSWERS)
    del payload["pre_conf_interview"]
    assert student_api.post(f"/api/interviews/{sid}/surveys/pre", json=payload).status_code == 422
    assert captured == []


# --------------------------------------------------------------------------
# 14. Spoofed frontend NUID or case cannot bypass the rule
# --------------------------------------------------------------------------
def test_spoofed_record_id_or_extra_field_rejected(student_api, captured):
    """The client cannot supply record_id (NUID) or any non-REDCap field - the
    NUID is resolved server-side from the session's owner."""
    sid = _new_session(student_api)
    payload = {**PRE_ANSWERS, "record_id": "hacker", "not_a_field": 1}
    assert student_api.post(f"/api/interviews/{sid}/surveys/pre", json=payload).status_code == 422
    assert captured == []


def test_case_is_resolved_from_session_not_client(student_api, captured):
    """The REDCap case instance is derived from the SESSION's case, which the
    client cannot override (routes are session-scoped, no case param)."""
    sid = _new_session(student_api, SOFIA)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert captured[0]["redcap_event_name"] == "sofia_arm_1"


# ==========================================================================
# Case-level lifecycle (the core new behaviour)
# ==========================================================================
# 1. First Carly Pre creates exactly ONE Carly receipt.
def test_first_pre_creates_single_receipt(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    receipts = _receipts(engine, CARLY)
    assert len(receipts) == 1
    assert receipts[0].case_id == CARLY


# 2. Carly Pre success leaves overall IN_PROGRESS.
def test_pre_success_leaves_in_progress(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    r = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert r.json()["overallStatus"] == "in_progress"
    assert _receipts(engine, CARLY)[0].overall_status == "in_progress"
    status = student_api.get(f"/api/interviews/{sid}/surveys/status").json()
    assert status["overallStatus"] == "in_progress"
    assert status["preSubmitted"] is True and status["postSubmitted"] is False


# 3. Carly Pre cannot be re-imported after it is already synced (idempotent).
def test_pre_not_reimported_after_sync(student_api, captured):
    sid = _new_session(student_api, CARLY)
    first = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert first.json()["alreadySubmitted"] is False
    second = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert second.status_code == 200
    assert second.json()["alreadySubmitted"] is True
    assert len(captured) == 1  # no duplicate REDCap write


# 4. A NEW Carly interview session does NOT create a second Carly receipt.
def test_new_session_same_case_reuses_receipt(student_api, captured, engine):
    sid_a = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid_a}/surveys/pre", json=PRE_ANSWERS)

    sid_b = _new_session(student_api, CARLY)  # brand-new session, same student+case
    assert sid_b != sid_a
    status_b = student_api.get(f"/api/interviews/{sid_b}/surveys/status").json()
    # The case-level receipt is visible through the new session too.
    assert status_b["preSubmitted"] is True
    assert status_b["overallStatus"] == "in_progress"
    # Re-submitting Pre through the new session does NOT duplicate anything.
    again = student_api.post(f"/api/interviews/{sid_b}/surveys/pre", json=PRE_ANSWERS)
    assert again.json()["alreadySubmitted"] is True
    assert len(_receipts(engine, CARLY)) == 1
    assert len(captured) == 1


# 5 & 6. Carly Post uses the same receipt and completes the package.
def test_post_uses_same_receipt_and_completes(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    r = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert r.status_code == 200, r.text
    assert r.json()["overallStatus"] == "completed"

    receipts = _receipts(engine, CARLY)
    assert len(receipts) == 1  # same single receipt, now completed
    rec = receipts[0]
    assert rec.overall_status == "completed"
    assert rec.pre_sync_status == "synced" and rec.post_sync_status == "synced"
    # Pre and Post routed to the SAME per-case REDCap event (one case instance).
    assert captured[0]["redcap_event_name"] == "carly_arm_1"
    assert captured[1]["redcap_event_name"] == "carly_arm_1"


# Pre-required gate: Post can never complete a package before Pre succeeds.
def test_post_without_pre_rejected_and_no_redcap_call(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    r = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "survey_pre_required"
    # REDCap was never called for the invalid Post ...
    assert captured == []
    # ... and nothing became completed (no phantom side effect at all).
    assert _receipts(engine, CARLY) == []
    status = student_api.get(f"/api/interviews/{sid}/surveys/status").json()
    assert status["overallStatus"] != "completed"


def test_pre_failed_then_post_rejected_without_redcap(engine, fake_client, monkeypatch):
    """Pre failed (not successful) -> Post is rejected by the Pre-required gate
    BEFORE any REDCap call, and the package never completes."""
    from app.services.redcap_client import RedcapError

    calls: list[dict] = []

    def _import(fields):
        calls.append(dict(fields))  # record the attempt ...
        raise RedcapError("boom")   # ... then fail (so Pre never succeeds)

    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", _import)

    with make_client(engine, fake_client, authenticate=True) as c:
        sid = _new_session(c, CARLY)
        assert c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 502
        assert len(calls) == 1  # the failed Pre attempt

        post = c.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
        assert post.status_code == 409
        assert post.json()["error"]["code"] == "survey_pre_required"
        # The gate short-circuited BEFORE REDCap: no additional import for Post.
        assert len(calls) == 1
        status = c.get(f"/api/interviews/{sid}/surveys/status").json()
        assert status["overallStatus"] == "in_progress"  # never completed
        assert status["postSyncStatus"] in (None, "pending")


def test_pre_success_then_post_success_completes(student_api, captured, engine):
    """The only path to COMPLETED: Pre successful AND Post successful."""
    sid = _new_session(student_api, CARLY)
    assert student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    post = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert post.status_code == 200
    assert post.json()["overallStatus"] == "completed"
    rec = _receipts(engine, CARLY)[0]
    assert rec.pre_sync_status == "synced" and rec.post_sync_status == "synced"
    assert rec.overall_status == "completed"


# 7. After Carly is COMPLETED, any further Carly attempt returns 409.
def test_completed_case_blocks_further_attempts(student_api, captured):
    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)

    # Same session, Post again -> 409.
    again = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "survey_already_completed"
    assert "Carly" in again.json()["error"]["message"]

    # A brand-NEW Carly session cannot bypass it either (case-level rule).
    sid2 = _new_session(student_api, CARLY)
    pre_again = student_api.post(f"/api/interviews/{sid2}/surveys/pre", json=PRE_ANSWERS)
    assert pre_again.status_code == 409
    assert pre_again.json()["error"]["code"] == "survey_already_completed"


def test_completed_carly_status_is_case_scoped_for_same_student(
    student_api, captured, engine
):
    """A new repeat-interview session sees Carly's completed package, while
    Camden remains independent and the repeated case itself is still allowed."""
    first_carly = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{first_carly}/surveys/pre", json=PRE_ANSWERS)
    student_api.post(f"/api/interviews/{first_carly}/surveys/post", json=POST_ANSWERS)

    repeat_carly = _new_session(student_api, CARLY)
    assert repeat_carly != first_carly  # the repeat interview was not blocked
    carly_status = student_api.get(
        f"/api/interviews/{repeat_carly}/surveys/status"
    ).json()
    assert carly_status["overallStatus"] == "completed"
    assert carly_status["caseName"] == "Carly"

    camden = _new_session(student_api, CAMDEN)
    camden_status = student_api.get(f"/api/interviews/{camden}/surveys/status").json()
    assert camden_status["overallStatus"] == "not_started"
    assert camden_status["preSubmitted"] is False
    assert len(_receipts(engine)) == 1


def test_another_student_does_not_inherit_completed_carly(engine, fake_client, captured):
    """Completion is resolved from the authenticated session owner, never a
    frontend-provided student number."""
    with make_client(engine, fake_client, authenticate=False) as c:
        student_a = auth_headers(c, email="survey-a@school.edu", number="SURVEY-A")
        student_b = auth_headers(c, email="survey-b@school.edu", number="SURVEY-B")

        a_session = _new_session(c, CARLY, headers=student_a)
        assert c.post(
            f"/api/interviews/{a_session}/surveys/pre",
            json=PRE_ANSWERS,
            headers=student_a,
        ).status_code == 200
        assert c.post(
            f"/api/interviews/{a_session}/surveys/post",
            json=POST_ANSWERS,
            headers=student_a,
        ).status_code == 200

        b_session = _new_session(c, CARLY, headers=student_b)
        b_status = c.get(
            f"/api/interviews/{b_session}/surveys/status", headers=student_b
        ).json()
        assert b_status["overallStatus"] == "not_started"
        assert b_status["preSubmitted"] is False
        assert b_status["postSubmitted"] is False


def test_completed_repeat_access_is_immutable_and_never_calls_redcap(
    student_api, captured, engine
):
    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    receipt = _receipts(engine, CARLY)[0]
    original = {
        "id": receipt.id,
        "latest_session_id": receipt.latest_session_id,
        "pre_synced_at": receipt.pre_synced_at,
        "post_synced_at": receipt.post_synced_at,
        "updated_at": receipt.updated_at,
    }
    assert len(captured) == 2

    repeat_sid = _new_session(student_api, CARLY)
    status = student_api.get(f"/api/interviews/{repeat_sid}/surveys/status")
    assert status.status_code == 200
    assert status.json()["overallStatus"] == "completed"
    rejected = student_api.post(
        f"/api/interviews/{repeat_sid}/surveys/pre", json=PRE_ANSWERS
    )
    assert rejected.status_code == 409
    assert len(captured) == 2  # neither status nor rejected repeat reached REDCap

    receipts = _receipts(engine, CARLY)
    assert len(receipts) == 1
    unchanged = receipts[0]
    assert unchanged.id == original["id"]
    assert unchanged.latest_session_id == original["latest_session_id"]
    assert unchanged.pre_synced_at == original["pre_synced_at"]
    assert unchanged.post_synced_at == original["post_synced_at"]
    assert unchanged.updated_at == original["updated_at"]


# 8. Camden is still allowed after Carly is completed.
def test_other_case_allowed_after_one_completed(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)

    camden = _new_session(student_api, CAMDEN)
    r = student_api.post(f"/api/interviews/{camden}/surveys/pre", json=PRE_ANSWERS)
    assert r.status_code == 200, r.text
    assert r.json()["overallStatus"] == "in_progress"
    assert {rec.case_id for rec in _receipts(engine)} == {CARLY, CAMDEN}


# 9. All 4 cases can each have one independent survey package.
def test_all_four_cases_independent(student_api, captured, engine):
    for case in ALL_CASES:
        sid = _new_session(student_api, case)
        student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
        student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)

    receipts = _receipts(engine)
    assert len(receipts) == 4
    assert {r.case_id for r in receipts} == set(ALL_CASES)
    assert all(r.overall_status == "completed" for r in receipts)
    # Each case wrote to its OWN event: no shared event name across cases.
    events = {c["redcap_event_name"] for c in captured}
    assert events == {f"{case}_arm_1" for case in ALL_CASES}


# 10. Pre REDCap failure does not mark Pre synced (502, retryable).
def test_pre_failure_does_not_mark_synced(engine, fake_client, failing_redcap):
    with make_client(engine, fake_client, authenticate=True) as c:
        sid = _new_session(c, CARLY)
        r = c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "survey_sync_failed"
        status = c.get(f"/api/interviews/{sid}/surveys/status").json()
        assert status["preSyncStatus"] == "failed"
        assert status["overallStatus"] == "in_progress"


# 11. Post REDCap failure does not mark Post synced or overall COMPLETED.
def test_post_failure_does_not_complete(engine, fake_client, monkeypatch):
    from app.services.redcap_client import RedcapError

    state = {"fail_on": "post"}
    calls: list[dict] = []

    def _import(fields):
        # Pre succeeds; Post fails.
        if "post_experience_survey_complete" in fields and state["fail_on"] == "post":
            raise RedcapError("boom")
        calls.append(dict(fields))

    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", _import)

    with make_client(engine, fake_client, authenticate=True) as c:
        sid = _new_session(c, CARLY)
        assert c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 200
        r = c.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
        assert r.status_code == 502
        status = c.get(f"/api/interviews/{sid}/surveys/status").json()
        assert status["postSyncStatus"] == "failed"
        assert status["overallStatus"] == "in_progress"  # NOT completed


# 12. Retry after a REDCap failure works.
def test_retry_after_failure_succeeds(engine, fake_client, monkeypatch):
    from app.services.redcap_client import RedcapError

    state = {"fail": True}
    calls: list[dict] = []

    def _import(fields):
        if state["fail"]:
            raise RedcapError("boom")
        calls.append(dict(fields))

    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", _import)

    with make_client(engine, fake_client, authenticate=True) as c:
        sid = _new_session(c, CARLY)
        assert c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 502
        state["fail"] = False
        retry = c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
        assert retry.status_code == 200
        assert retry.json()["syncStatus"] == "synced"
        assert len(calls) == 1  # exactly one successful write, no duplicate record


# 13. Concurrency / duplicate protection: UNIQUE(student_id, case_id) at DB level.
def test_unique_constraint_blocks_duplicate_receipts(engine):
    from app.models import Student, SurveyReceipt

    db = sessionmaker(bind=engine)()
    try:
        student = db.query(Student).first()
        if student is None:
            student = Student(name="Dup Test", student_number="DUP1", email="dup@school.edu")
            db.add(student)
            db.commit()
        db.add(SurveyReceipt(student_id=student.id, case_id=CARLY, redcap_record_id="D1"))
        db.commit()
        db.add(SurveyReceipt(student_id=student.id, case_id=CARLY, redcap_record_id="D1"))
        with pytest.raises(IntegrityError):
            db.commit()  # second (student_id, case_id) row is rejected by the DB
    finally:
        db.rollback()
        db.close()


# 15. No survey answers are persisted locally.
def test_no_answers_persisted_locally(student_api, captured, engine):
    from sqlalchemy import inspect as sa_inspect

    from app.models import SurveyReceipt

    sid = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)

    # (a) The table has ONLY metadata columns - no answer variable columns.
    cols = {c.name for c in sa_inspect(SurveyReceipt).columns}
    assert cols == {
        "id", "student_id", "case_id", "latest_session_id", "redcap_record_id",
        "pre_sync_status", "pre_synced_at", "post_sync_status", "post_synced_at",
        "overall_status", "created_at", "updated_at",
    }
    # No answer/likert/open-ended field ever became a column.
    answer_keys = set(PRE_ANSWERS) | set(POST_ANSWERS)
    assert cols.isdisjoint(answer_keys)

    # (b) No stored value equals any submitted answer (Likert ints or OE text).
    rec = _receipts(engine, CARLY)[0]
    stored = {getattr(rec, c) for c in cols}
    submitted_values = {v for v in (PRE_ANSWERS | POST_ANSWERS).values() if v != ""}
    assert stored.isdisjoint(submitted_values)


# --------------------------------------------------------------------------
# Missing NUID: never send empty record_id
# --------------------------------------------------------------------------
def test_missing_nuid_blocks_submission(engine, fake_client, captured):
    with make_client(engine, fake_client, authenticate=True) as c:
        sid = _new_session(c, CARLY)
        _set_default_student_number(engine, "")
        r = c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "nuid_missing"
        assert captured == []
        status = c.get(f"/api/interviews/{sid}/surveys/status").json()
        assert status["nuidOnFile"] is False


# --------------------------------------------------------------------------
# Unconfigured REDCap (dev/CI): validated + recorded locally as "skipped",
# and STILL follows the case-level lifecycle (post skipped -> completed).
# --------------------------------------------------------------------------
def test_unconfigured_redcap_skips_but_still_completes(student_api):
    sid = _new_session(student_api, CARLY)
    pre = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert pre.json()["syncStatus"] == "skipped"
    assert pre.json()["overallStatus"] == "in_progress"
    post = student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    assert post.json()["syncStatus"] == "skipped"
    assert post.json()["overallStatus"] == "completed"
    # Completed even when skipped -> re-entry blocked.
    assert student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 409


# --------------------------------------------------------------------------
# Ownership / isolation + token never in responses
# --------------------------------------------------------------------------
def test_student_cannot_submit_for_another_students_session(engine, fake_client):
    with make_client(engine, fake_client, authenticate=False) as c:
        ha = auth_headers(c, email="owner@school.edu", number="A1")
        hb = auth_headers(c, email="other@school.edu", number="B2")
        sid = _new_session(c, CARLY, headers=ha)
        r = c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS, headers=hb)
        assert r.status_code == 404  # same 404 as nonexistent - no existence leak
        assert c.get(f"/api/interviews/{sid}/surveys/status", headers=hb).status_code == 404


def test_survey_endpoints_require_auth(engine, fake_client):
    with make_client(engine, fake_client, authenticate=False) as c:
        h = auth_headers(c, email="me@school.edu", number="M1")
        sid = _new_session(c, CARLY, headers=h)
        assert c.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS).status_code == 401
        assert c.get(f"/api/interviews/{sid}/surveys/status").status_code == 401


def test_responses_never_contain_redcap_token(student_api, captured):
    sid = _new_session(student_api, CARLY)
    submit = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    status = student_api.get(f"/api/interviews/{sid}/surveys/status")
    for resp in (submit, status):
        assert "token" not in resp.text.lower()
