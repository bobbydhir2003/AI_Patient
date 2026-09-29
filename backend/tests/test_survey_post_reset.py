"""Independent Post reset: an admin "Reset Post" reopens the Post stage to ANY
case (first successful Post claims it) while the completed Pre - and its owner
case - stays untouched. Without an admin Post reset, behavior is unchanged
(Post only on the Pre owner case). REDCap I/O is intercepted (no network)."""
from datetime import datetime, timezone

from alembic import command
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.models import Student, SurveyReceipt
from tests.test_student_data import (  # noqa: F401 - fixtures
    _complete_survey,
    _default_student_id,
    _factory,
    _receipts_for,
    _reset,
    _resets_for,
    _status,
    api,
    redcap,
    superadmin,
)
from tests.test_survey_migration import _alembic_config
from tests.test_surveys import (
    CARLY,
    CASE_NUMBER,
    DEFAULT_NUID,
    JAYDEN,
    POST_ANSWERS,
    PRE_ANSWERS,
    SOFIA,
    _new_session,
)


def _post(api, sid):
    return api.post(f"/api/interviews/{sid}/surveys/post", json=POST_ANSWERS)


def _pre(api, sid):
    return api.post(f"/api/interviews/{sid}/surveys/pre", json=PRE_ANSWERS)


def _student(engine):
    db = _factory(engine)()
    try:
        return db.get(Student, _default_student_id(engine))
    finally:
        db.close()


def _receipt(engine, case):
    return next((r for r in _receipts_for(engine, _default_student_id(engine)) if r.case_id == case), None)


def _code(r):
    return r.json()["error"]["code"]


# ------------------------------------------------------------------ normal flow
def test_normal_flow_carly_pre_and_post_unchanged(api, engine, redcap):
    sid = _new_session(api, CARLY)
    st = _status(api, sid)
    assert (st["preGate"], st["postGate"]) == ("collect", "skip")
    assert _pre(api, sid).status_code == 200
    assert _status(api, sid)["postGate"] == "collect"
    assert _post(api, sid).status_code == 200
    st = _status(api, sid)
    assert st["globalSurveyStatus"] == "completed"
    assert (st["preCaseId"], st["postCaseId"], st["postReopened"]) == (CARLY, CARLY, False)
    assert (st["preGate"], st["postGate"]) == ("continue", "skip")
    rec = _receipt(engine, CARLY)
    assert rec.pre_redcap_record_id == rec.post_redcap_record_id == rec.redcap_record_id
    assert _student(engine).survey_post_case_id == CARLY


def test_normal_flow_post_from_other_case_is_blocked(api, engine, redcap):
    carly = _new_session(api, CARLY)
    assert _pre(api, carly).status_code == 200
    sofia = _new_session(api, SOFIA)
    st = _status(api, sofia)
    assert (st["preGate"], st["postGate"]) == ("skip", "skip")
    r = _post(api, sofia)
    assert r.status_code == 409 and _code(r) == "survey_owned_by_other_case"
    assert _receipt(engine, SOFIA) is None and len(redcap) == 1


# ------------------------------------------------------------- post reset (main)
def test_post_reset_then_post_from_sofia(api, superadmin, engine, redcap):
    carly_sid = _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    original = _receipt(engine, CARLY)
    original_record = original.redcap_record_id
    pre_synced_at = original.pre_synced_at

    r = _reset(api, superadmin, student_id, "post")
    assert r.status_code == 200, r.text
    sv = r.json()["survey"]
    assert sv["postReopened"] is True and sv["pre"]["completed"] is True
    assert sv["preCaseName"] == "Carly" and sv["postCaseId"] is None

    # Sofia: Pre skipped (already completed with Carly), Post collected.
    sofia = _new_session(api, SOFIA)
    st = _status(api, sofia)
    assert (st["preGate"], st["postGate"]) == ("skip", "collect")
    assert st["preCompleted"] is True and st["preCaseName"] == "Carly" and st["postReopened"] is True
    redcap.clear()
    r = _pre(api, sofia)
    assert r.status_code == 409 and redcap == []  # Pre never re-asked / re-sent

    assert _post(api, sofia).status_code == 200
    [fields] = redcap
    sofia_rec = _receipt(engine, SOFIA)
    assert fields["nuid"] == DEFAULT_NUID
    assert fields["case_id"] == CASE_NUMBER[SOFIA] == "3"
    assert fields["redcap_event_name"] == "sofia_arm_1"
    assert fields["record_id"] == sofia_rec.redcap_record_id
    assert fields["record_id"] not in (original_record, _receipt(engine, CARLY).redcap_record_id)
    assert fields["post_experience_survey_complete"] == 2
    assert not any(k.startswith("pre_") for k in fields)
    assert sofia_rec.post_redcap_record_id == fields["record_id"]
    assert sofia_rec.pre_sync_status == "pending"

    # Carly Pre untouched: still done, still on the original REDCap record.
    carly = _receipt(engine, CARLY)
    assert carly.pre_sync_status == "synced" and carly.pre_synced_at == pre_synced_at
    assert carly.pre_redcap_record_id == original_record

    st = _status(api, sofia)
    assert (st["preCaseId"], st["postCaseId"]) == (CARLY, SOFIA)
    assert (st["preCaseName"], st["postCaseName"]) == ("Carly", "Sofia")
    assert st["globalSurveyStatus"] == "completed" and st["postReopened"] is False
    s = _student(engine)
    assert (s.survey_owner_case_id, s.survey_post_case_id) == (CARLY, SOFIA)
    assert s.survey_completed_at is not None and s.survey_post_reopened_at is None

    # Later cases (and Carly again) skip both.
    for sid in (_new_session(api, JAYDEN), carly_sid):
        st = _status(api, sid)
        assert st["postGate"] == "skip"
        assert _post(api, sid).status_code == 409

    # Admin view shows the split.
    detail = api.get(f"/api/admin/student-data/{student_id}", headers=superadmin).json()["survey"]
    assert (detail["preCaseName"], detail["postCaseName"]) == ("Carly", "Sofia")
    assert detail["globalStatus"] == "completed" and detail["canResetPost"] is True


def test_post_reset_then_post_carly_again_uses_fresh_record(api, superadmin, engine, redcap):
    sid = _complete_survey(api, CARLY)
    old = _receipt(engine, CARLY).redcap_record_id
    assert _reset(api, superadmin, _default_student_id(engine), "post").status_code == 200
    assert _status(api, sid)["postGate"] == "collect"
    redcap.clear()
    assert _post(api, sid).status_code == 200
    assert redcap[0]["record_id"] != old and redcap[0]["case_id"] == CASE_NUMBER[CARLY]
    rec = _receipt(engine, CARLY)
    assert rec.pre_redcap_record_id == old  # Pre still linked to its original record
    assert rec.post_redcap_record_id == redcap[0]["record_id"]
    assert _status(api, sid)["globalSurveyStatus"] == "completed"


def test_double_click_reopened_post_is_not_resubmitted(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    assert _reset(api, superadmin, _default_student_id(engine), "post").status_code == 200
    sofia = _new_session(api, SOFIA)
    redcap.clear()
    assert _post(api, sofia).status_code == 200
    r = _post(api, sofia)
    assert r.status_code == 409 and _code(r) == "survey_already_completed"
    assert len(redcap) == 1


def test_competing_reopened_post_claim_only_one_wins(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    sofia = _new_session(api, SOFIA)
    jayden = _new_session(api, JAYDEN)
    # Simulate Sofia's claim being in flight (committed before its REDCap call).
    db = _factory(engine)()
    try:
        db.get(Student, student_id).survey_post_case_id = SOFIA
        db.commit()
    finally:
        db.close()
    assert _status(api, jayden)["postGate"] == "skip"
    redcap.clear()
    r = _post(api, jayden)
    assert r.status_code == 409 and _code(r) == "survey_owned_by_other_case"
    assert redcap == [] and _receipt(engine, JAYDEN) is None
    # Sofia (the claim holder) completes; Jayden is still rejected afterwards.
    assert _post(api, sofia).status_code == 200
    assert _post(api, jayden).status_code == 409
    assert _student(engine).survey_post_case_id == SOFIA


def test_failed_reopened_post_releases_claim_and_can_retry_elsewhere(
    api, superadmin, engine, redcap, monkeypatch
):
    from app.services.redcap_client import RedcapError

    _complete_survey(api, CARLY)
    assert _reset(api, superadmin, _default_student_id(engine), "post").status_code == 200

    def _boom(_fields):
        raise RedcapError("boom")

    monkeypatch.setattr("app.services.redcap_client.import_record", _boom)
    sofia = _new_session(api, SOFIA)
    assert _post(api, sofia).status_code == 502
    s = _student(engine)
    assert s.survey_post_case_id is None and s.survey_post_reopened_at is not None
    assert _receipt(engine, SOFIA).post_sync_status == "failed"

    calls: list[dict] = []
    monkeypatch.setattr("app.services.redcap_client.import_record", lambda f: calls.append(dict(f)))
    jayden = _new_session(api, JAYDEN)
    assert _status(api, jayden)["postGate"] == "collect"
    assert _post(api, jayden).status_code == 200
    assert calls[0]["case_id"] == CASE_NUMBER[JAYDEN]
    assert _student(engine).survey_post_case_id == JAYDEN


def test_repeated_post_reset_targets_current_post_case(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    sofia = _new_session(api, SOFIA)
    assert _post(api, sofia).status_code == 200
    sofia_record = _receipt(engine, SOFIA).redcap_record_id

    r = _reset(api, superadmin, student_id, "post")
    assert r.status_code == 200, r.text
    snaps = sorted(_resets_for(engine, student_id), key=lambda x: x.reset_at)
    assert [(x.scope, x.case_id) for x in snaps] == [("post", CARLY), ("post", SOFIA)]
    assert snaps[-1].redcap_record_id == sofia_record
    sofia_rec = _receipt(engine, SOFIA)
    assert sofia_rec.post_sync_status == "pending" and sofia_rec.redcap_record_id != sofia_record
    assert _student(engine).survey_post_reopened_at is not None

    carly = _new_session(api, CARLY)
    redcap.clear()
    assert _post(api, carly).status_code == 200
    assert redcap[0]["record_id"] == _receipt(engine, CARLY).redcap_record_id
    assert _status(api, carly)["globalSurveyStatus"] == "completed"


def test_legacy_per_case_completed_receipt_is_never_overwritten(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    db = _factory(engine)()
    try:
        db.add(SurveyReceipt(
            student_id=student_id, case_id=SOFIA, redcap_record_id="legacy-sofia",
            pre_sync_status="synced", post_sync_status="synced", overall_status="completed",
        ))
        db.commit()
    finally:
        db.close()
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    sofia = _new_session(api, SOFIA)
    assert _status(api, sofia)["postGate"] == "skip"
    redcap.clear()
    assert _post(api, sofia).status_code == 409 and redcap == []


# ------------------------------------------------------------ pre / both resets
def test_pre_reset_keeps_owner_case_and_post_page_skips(api, superadmin, engine, redcap):
    sid = _complete_survey(api, CARLY)
    assert _reset(api, superadmin, _default_student_id(engine), "pre").status_code == 200
    st = _status(api, sid)
    # Pre retake stays with Carly; Post is already done -> never re-collected.
    assert (st["preGate"], st["postGate"]) == ("collect", "skip")
    assert _status(api, _new_session(api, SOFIA))["preGate"] == "skip"
    assert _student(engine).survey_post_reopened_at is None
    redcap.clear()
    assert _pre(api, sid).status_code == 200
    assert redcap[0]["case_id"] == CASE_NUMBER[CARLY]
    assert _status(api, sid)["globalSurveyStatus"] == "completed"


def test_pre_reset_after_split_completion(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    assert _post(api, _new_session(api, SOFIA)).status_code == 200
    assert _reset(api, superadmin, student_id, "pre").status_code == 200
    carly = _new_session(api, CARLY)
    assert _status(api, carly)["postGate"] == "skip"
    assert _pre(api, carly).status_code == 200
    st = _status(api, carly)
    assert st["globalSurveyStatus"] == "completed"
    assert (st["preCaseId"], st["postCaseId"]) == (CARLY, SOFIA)


def test_reset_both_returns_to_normal_rules(api, superadmin, engine, redcap):
    _complete_survey(api, CARLY)
    student_id = _default_student_id(engine)
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    assert _reset(api, superadmin, student_id, "both").status_code == 200
    s = _student(engine)
    assert s.survey_post_case_id is None and s.survey_post_reopened_at is None
    jayden = _new_session(api, JAYDEN)
    assert _pre(api, jayden).status_code == 200
    sofia = _new_session(api, SOFIA)
    assert _status(api, sofia)["postGate"] == "skip"
    assert _post(api, sofia).status_code == 409  # no cross-case Post without a Post reset
    assert _post(api, jayden).status_code == 200


# ------------------------------------------------------------- legacy student
def test_legacy_student_without_post_pointer_behaves_as_before(api, superadmin, engine, redcap):
    student_id = _default_student_id(engine)
    db = _factory(engine)()
    try:
        s = db.get(Student, student_id)
        s.survey_owner_case_id = CARLY
        s.survey_completed_at = datetime.now(timezone.utc)
        db.add(SurveyReceipt(
            student_id=student_id, case_id=CARLY, redcap_record_id="legacy-carly",
            pre_sync_status="synced", post_sync_status="synced", overall_status="completed",
        ))
        db.commit()
    finally:
        db.close()
    carly = _new_session(api, CARLY)
    st = _status(api, carly)
    assert (st["postCaseId"], st["postGate"], st["preGate"]) == (CARLY, "skip", "continue")
    assert _status(api, _new_session(api, SOFIA))["preGate"] == "skip"
    # Post reset falls back to the owner receipt.
    assert _reset(api, superadmin, student_id, "post").status_code == 200
    [snap] = _resets_for(engine, student_id)
    assert (snap.case_id, snap.redcap_record_id) == (CARLY, "legacy-carly")


# ---------------------------------------------------------------- migration 0027
def test_migration_0027_backfill(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE students (id VARCHAR(32) PRIMARY KEY, survey_owner_case_id VARCHAR(50), "
            "survey_completed_at DATETIME)"
        ))
        c.execute(text(
            "CREATE TABLE survey_receipts (id VARCHAR(32) PRIMARY KEY, student_id VARCHAR(32), "
            "case_id VARCHAR(50), redcap_record_id VARCHAR(100), pre_sync_status VARCHAR(20), "
            "pre_synced_at DATETIME, post_sync_status VARCHAR(20), post_synced_at DATETIME)"
        ))
        c.execute(text(
            "CREATE TABLE survey_receipt_resets (id VARCHAR(32) PRIMARY KEY, student_id VARCHAR(32), "
            "case_id VARCHAR(50), scope VARCHAR(10), redcap_record_id VARCHAR(100), reset_at DATETIME)"
        ))
        # done: completed normally. reopened: Post reset pending. fresh: Pre only.
        c.execute(text(
            "INSERT INTO students VALUES ('done','carly','2026-01-03 00:00:00'),"
            "('reopened','carly',NULL),('fresh','carly',NULL)"
        ))
        c.execute(text(
            "INSERT INTO survey_receipts VALUES "
            "('r1','done','carly','R1','synced','2026-01-01 00:00:00','synced','2026-01-02 00:00:00'),"
            "('r2','reopened','carly','R2b','synced','2026-01-01 00:00:00','pending',NULL),"
            "('r3','fresh','carly','R3','synced','2026-01-01 00:00:00','pending',NULL)"
        ))
        c.execute(text(
            "INSERT INTO survey_receipt_resets VALUES "
            "('x1','reopened','carly','post','R2a','2026-01-05 00:00:00')"
        ))
    cfg = _alembic_config(url)
    command.stamp(cfg, "0026")
    command.upgrade(cfg, "0027")
    with eng.connect() as c:
        st = {r.id: r for r in c.execute(text("SELECT * FROM students"))}
        rc = {r.id: r for r in c.execute(text("SELECT * FROM survey_receipts"))}
    assert st["done"].survey_post_case_id == "carly" and st["done"].survey_post_reopened_at is None
    assert st["reopened"].survey_post_case_id is None and st["reopened"].survey_post_reopened_at is not None
    assert st["fresh"].survey_post_case_id is None and st["fresh"].survey_post_reopened_at is None
    assert (rc["r1"].pre_redcap_record_id, rc["r1"].post_redcap_record_id) == ("R1", "R1")
    # Pre synced before the Post reset rotated R2a -> R2b: Pre lives on R2a.
    assert (rc["r2"].pre_redcap_record_id, rc["r2"].post_redcap_record_id) == ("R2a", None)
    command.downgrade(cfg, "0026")
    with eng.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(students)"))}
    assert "survey_post_case_id" not in cols
    eng.dispose()
    get_settings.cache_clear()
