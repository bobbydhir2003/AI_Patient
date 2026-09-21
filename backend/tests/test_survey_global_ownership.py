"""Global (one-package-per-student) survey ownership tests.

The survey is GLOBAL per student: the first case whose Pre successfully reaches
REDCap becomes the permanent survey owner; every other case is gated to
"already submitted / skip". Covers the redesign scenarios C (non-owner while
owner in progress), J (concurrent first Pre -> one owner), K (failed first Pre
does not permanently lock), plus the global status transitions and idempotency.

REDCap I/O is intercepted at the redcap_client boundary (no network).
"""
import pytest

from tests.conftest import make_client
from tests.test_surveys import (
    CAMDEN,
    CARLY,
    SOFIA,
    POST_ANSWERS,
    PRE_ANSWERS,
    _new_session,
    _receipts,
)


@pytest.fixture()
def captured(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.redcap_client.import_record", lambda fields: calls.append(dict(fields))
    )
    return calls


@pytest.fixture()
def student_api(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        yield c


def _status(client, sid):
    return client.get(f"/api/interviews/{sid}/surveys/status").json()


# --------------------------------------------------------------------------
# Global status transitions: NOT_STARTED -> IN_PROGRESS -> COMPLETED
# --------------------------------------------------------------------------
def test_global_status_transitions(student_api, captured):
    sid = _new_session(student_api, CARLY)
    s0 = _status(student_api, sid)
    assert s0["globalSurveyStatus"] == "not_started"
    assert s0["surveyOwnerCaseId"] is None
    assert s0["isSurveyOwnerCase"] is False

    student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    s1 = _status(student_api, sid)
    assert s1["globalSurveyStatus"] == "in_progress"
    assert s1["surveyOwnerCaseId"] == CARLY
    assert s1["isSurveyOwnerCase"] is True
    assert s1["globalSurveyCompleted"] is False

    student_api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)
    s2 = _status(student_api, sid)
    assert s2["globalSurveyStatus"] == "completed"
    assert s2["globalSurveyCompleted"] is True
    assert s2["surveyOwnerCaseId"] == CARLY


# --------------------------------------------------------------------------
# C. Non-owner while owner IN_PROGRESS: Pre and Post both skip; no package.
# --------------------------------------------------------------------------
def test_non_owner_skipped_while_owner_in_progress(student_api, captured, engine):
    carly = _new_session(student_api, CARLY)
    student_api.post(f"/api/interviews/{carly}/surveys/pre", json=PRE_ANSWERS)  # Carly owns, IN_PROGRESS

    sofia = _new_session(student_api, SOFIA)
    status = _status(student_api, sofia)
    assert status["globalSurveyStatus"] == "in_progress"
    assert status["isSurveyOwnerCase"] is False
    assert status["surveyOwnerCaseId"] == CARLY

    # Backstop: Sofia Pre AND Post are both rejected as owned elsewhere.
    pre = student_api.post(f"/api/interviews/{sofia}/surveys/pre", json=PRE_ANSWERS)
    assert pre.status_code == 409 and pre.json()["error"]["code"] == "survey_owned_by_other_case"
    post = student_api.post(f"/api/interviews/{sofia}/surveys/post", json=POST_ANSWERS)
    assert post.status_code == 409 and post.json()["error"]["code"] == "survey_owned_by_other_case"
    # No Sofia package was ever established, and only Carly reached REDCap.
    assert {r.case_id for r in _receipts(engine)} == {CARLY}
    assert len(captured) == 1


# --------------------------------------------------------------------------
# J. Concurrent first Pre from two cases -> exactly one owner (serialized).
# The SQLite test store cannot truly run in parallel; we assert the invariant
# that once one case claims ownership, the other is deterministically rejected
# (the Student-row FOR UPDATE provides real serialization in Postgres).
# --------------------------------------------------------------------------
def test_concurrent_first_pre_yields_single_owner(student_api, captured, engine):
    carly = _new_session(student_api, CARLY)
    sofia = _new_session(student_api, SOFIA)
    # Carly's Pre wins the claim first.
    assert student_api.post(f"/api/interviews/{carly}/surveys/pre", json=PRE_ANSWERS).status_code == 200
    # Sofia's near-simultaneous Pre now observes the owner and is rejected.
    r = student_api.post(f"/api/interviews/{sofia}/surveys/pre", json=PRE_ANSWERS)
    assert r.status_code == 409 and r.json()["error"]["code"] == "survey_owned_by_other_case"
    receipts = _receipts(engine)
    assert len(receipts) == 1 and receipts[0].case_id == CARLY  # never two owners


# --------------------------------------------------------------------------
# K. Failed FIRST Pre must not permanently lock the student to that case.
# --------------------------------------------------------------------------
def test_failed_first_pre_releases_ownership_allowing_retry_elsewhere(engine, fake_client, monkeypatch):
    from app.services.redcap_client import RedcapError

    state = {"fail": True}
    calls: list[dict] = []

    def _import(fields):
        calls.append(dict(fields))
        if state["fail"]:
            raise RedcapError("boom")

    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", _import)

    with make_client(engine, fake_client, authenticate=True) as c:
        carly = _new_session(c, CARLY)
        # First Pre for Carly fails at REDCap -> ownership must be released.
        assert c.post(f"/api/interviews/{carly}/surveys/pre", json=PRE_ANSWERS).status_code == 502
        assert _status(c, carly)["globalSurveyStatus"] == "not_started"
        assert _status(c, carly)["surveyOwnerCaseId"] is None

        # The student is free to establish the package on a DIFFERENT case now.
        state["fail"] = False
        camden = _new_session(c, CAMDEN)
        assert c.post(f"/api/interviews/{camden}/surveys/pre", json=PRE_ANSWERS).status_code == 200
        assert _status(c, camden)["surveyOwnerCaseId"] == CAMDEN
        assert _status(c, camden)["globalSurveyStatus"] == "in_progress"


def test_failed_first_pre_then_retry_same_case_still_works(engine, fake_client, monkeypatch):
    from app.services.redcap_client import RedcapError

    state = {"fail": True}
    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)

    def _import(fields):
        if state["fail"]:
            raise RedcapError("boom")

    monkeypatch.setattr("app.services.redcap_client.import_record", _import)

    with make_client(engine, fake_client, authenticate=True) as c:
        carly = _new_session(c, CARLY)
        assert c.post(f"/api/interviews/{carly}/surveys/pre", json=PRE_ANSWERS).status_code == 502
        state["fail"] = False
        assert c.post(f"/api/interviews/{carly}/surveys/pre", json=PRE_ANSWERS).status_code == 200
        assert _status(c, carly)["surveyOwnerCaseId"] == CARLY


# --------------------------------------------------------------------------
# L/M. Idempotent double-submit of the OWNING case (no duplicate package).
# --------------------------------------------------------------------------
def test_owner_double_submit_is_idempotent(student_api, captured, engine):
    sid = _new_session(student_api, CARLY)
    first = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert first.json()["alreadySubmitted"] is False
    second = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert second.status_code == 200 and second.json()["alreadySubmitted"] is True
    assert len(captured) == 1
    assert len(_receipts(engine, CARLY)) == 1


# --------------------------------------------------------------------------
# Unconfigured REDCap (skipped) still establishes global ownership.
# --------------------------------------------------------------------------
def test_skipped_pre_establishes_ownership(student_api):
    sid = _new_session(student_api, CARLY)
    pre = student_api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)
    assert pre.json()["syncStatus"] == "skipped"
    assert _status(student_api, sid)["surveyOwnerCaseId"] == CARLY
    # Non-owner case is skipped even under the unconfigured path.
    sofia = _new_session(student_api, SOFIA)
    r = student_api.post(f"/api/interviews/{sofia}/surveys/pre", json=PRE_ANSWERS)
    assert r.status_code == 409
