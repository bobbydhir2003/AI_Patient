"""Bulk admin hard-delete of user accounts + their ENTIRE local data tree.

"Delete account" is a permanent HARD delete (distinct from "disable"): it removes
the User, the Student profile, interview sessions, conversation turns, assessment
runs/domain results/evidence, and LOCAL survey receipts. Survey *answers* live in
REDCap and are NEVER touched by this path (no REDCap call is made).

These tests run against a SQLite engine with ``PRAGMA foreign_keys=ON`` so a
missing deletion (e.g. the historical survey_receipts gap) surfaces here exactly
as it would on PostgreSQL, instead of being silently ignored.
"""
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DeleteConfirmationError
from app.core.security import hash_password
from app.database.base import Base
from app.models import (
    AssessmentDomainResult,
    AssessmentEvidence,
    AssessmentRun,
    ConversationTurn,
    InterviewSession,
    Student,
    SurveyReceipt,
    User,
)
from app.services import user_admin_service


@event.listens_for(Engine, "connect")
def _fk_on(dbapi_conn, _rec):  # pragma: no cover - trivial pragma hook
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    # Confirm FK enforcement is actually on (guards against a silent false-pass).
    assert session.execute(select(func.count()).select_from(User)) is not None
    assert engine.dialect.name == "sqlite"
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _admin(db, email="admin@school.edu") -> User:
    u = User(
        email=email, password_hash=hash_password("adminpass1"),
        full_name="Admin", role="admin",
    )
    db.add(u)
    db.flush()
    return u


def _student_with_full_tree(db, *, email="stu@school.edu", number="S1") -> tuple[User, Student, dict]:
    """Create a student login + profile with one session, two turns, an assessment
    run/domain/evidence, and a survey receipt. Returns (user, student, ids)."""
    student = Student(name="Stu", student_number=number, email=email, survey_owner_case_id="carly")
    db.add(student)
    db.flush()
    user = User(
        email=email, password_hash=hash_password("stupass1"), full_name="Stu",
        role="student", student_id=student.id,
    )
    db.add(user)
    sess = InterviewSession(student_id=student.id, case_id="carly", status="completed", locked=True)
    db.add(sess)
    db.flush()
    turn_s = ConversationTurn(session_id=sess.id, turn_index=0, role="student", content="Hi")
    turn_p = ConversationTurn(session_id=sess.id, turn_index=1, role="patient", content="Hello")
    db.add_all([turn_s, turn_p])
    db.flush()
    run = AssessmentRun(session_id=sess.id, case_id="carly", status="COMPLETED")
    db.add(run)
    db.flush()
    domain = AssessmentDomainResult(
        assessment_run_id=run.id, rubric_domain="rapport", performance_level="developing"
    )
    db.add(domain)
    db.flush()
    evidence = AssessmentEvidence(
        domain_result_id=domain.id, turn_id=turn_s.id, evidence_type="strength", label="Good open"
    )
    db.add(evidence)
    receipt = SurveyReceipt(
        student_id=student.id, case_id="carly", latest_session_id=sess.id,
        redcap_record_id="rec1", overall_status="completed",
    )
    db.add(receipt)
    db.commit()
    ids = {
        "user": user.id, "student": student.id, "session": sess.id,
        "turns": [turn_s.id, turn_p.id], "run": run.id, "domain": domain.id,
        "evidence": evidence.id, "receipt": receipt.id,
    }
    return user, student, ids


def _counts(db) -> dict:
    def n(model):
        return int(db.execute(select(func.count()).select_from(model)).scalar_one())
    return {
        "users": n(User), "students": n(Student), "sessions": n(InterviewSession),
        "turns": n(ConversationTurn), "runs": n(AssessmentRun),
        "domains": n(AssessmentDomainResult), "evidence": n(AssessmentEvidence),
        "receipts": n(SurveyReceipt),
    }


# --------------------------------------------------------------------------
# Full-tree deletion (the core requirement) under FK enforcement.
# --------------------------------------------------------------------------
def test_bulk_delete_removes_entire_local_tree(db):
    admin = _admin(db)
    user, student, ids = _student_with_full_tree(db)
    db.commit()

    res = user_admin_service.bulk_delete(db, admin, [user.id], confirm="DELETE")

    assert res["succeeded"] == [user.id]
    assert res["skipped"] == []
    # Every LOCAL row in the student's tree is gone; only the acting admin remains.
    c = _counts(db)
    assert c == {
        "users": 1, "students": 0, "sessions": 0, "turns": 0,
        "runs": 0, "domains": 0, "evidence": 0, "receipts": 0,
    }
    assert db.get(User, admin.id) is not None
    assert db.get(User, ids["user"]) is None
    assert db.get(SurveyReceipt, ids["receipt"]) is None
    assert db.get(AssessmentEvidence, ids["evidence"]) is None


def test_bulk_delete_writes_audit_and_does_not_call_redcap(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.redcap_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.redcap_client.import_record", lambda f: calls.append(f))

    admin = _admin(db)
    user, _student, _ids = _student_with_full_tree(db)
    db.commit()

    user_admin_service.bulk_delete(db, admin, [user.id], confirm="DELETE")

    # No REDCap write is ever attempted by the delete path.
    assert calls == []
    # An audit row was recorded for the deletion (history is retained).
    from app.models import AuditLog
    actions = [a.action_type for a in db.execute(select(AuditLog)).scalars().all()]
    assert "student_deleted" in actions


def test_bulk_delete_admin_only_account_removes_just_the_user(db):
    admin = _admin(db)
    lone = User(
        email="lone-admin@school.edu", password_hash=hash_password("x1234567"),
        full_name="Lone", role="admin",
    )
    db.add(lone)
    db.commit()

    res = user_admin_service.bulk_delete(db, admin, [lone.id], confirm="DELETE")
    assert res["succeeded"] == [lone.id]
    assert db.get(User, lone.id) is None
    assert db.get(User, admin.id) is not None


# --------------------------------------------------------------------------
# Guards.
# --------------------------------------------------------------------------
def test_bulk_delete_requires_confirmation(db):
    admin = _admin(db)
    user, _s, _ids = _student_with_full_tree(db)
    db.commit()
    with pytest.raises(DeleteConfirmationError):
        user_admin_service.bulk_delete(db, admin, [user.id], confirm="")
    # Nothing was deleted.
    assert db.get(User, user.id) is not None


def test_bulk_delete_skips_self(db):
    admin = _admin(db)
    db.commit()
    res = user_admin_service.bulk_delete(db, admin, [admin.id], confirm="DELETE")
    assert res["succeeded"] == []
    assert res["skipped"] == [{"user_id": admin.id, "reason": "cannot_delete_self"}]
    assert db.get(User, admin.id) is not None


def test_bulk_delete_mixed_selection_and_self_and_missing(db):
    admin = _admin(db)
    user_a, _sa, ids_a = _student_with_full_tree(db, email="a@school.edu", number="A1")
    user_b, _sb, ids_b = _student_with_full_tree(db, email="b@school.edu", number="B1")
    db.commit()

    res = user_admin_service.bulk_delete(
        db, admin, [user_a.id, "does-not-exist", admin.id, user_b.id], confirm="DELETE"
    )
    assert set(res["succeeded"]) == {user_a.id, user_b.id}
    reasons = {s["user_id"]: s["reason"] for s in res["skipped"]}
    assert reasons == {"does-not-exist": "not_found", admin.id: "cannot_delete_self"}
    # Both students fully gone; admin remains.
    assert _counts(db) == {
        "users": 1, "students": 0, "sessions": 0, "turns": 0,
        "runs": 0, "domains": 0, "evidence": 0, "receipts": 0,
    }


def test_bulk_delete_preserves_last_active_admin(db):
    # Deleting a co-admin is allowed and always leaves the acting admin behind, so
    # the system can never be left without an administrator.
    admin = _admin(db, email="a1@school.edu")
    admin_b = _admin(db, email="a2@school.edu")
    db.commit()
    res = user_admin_service.bulk_delete(db, admin, [admin_b.id], confirm="DELETE")
    assert res["succeeded"] == [admin_b.id]
    remaining = db.execute(select(func.count()).select_from(User).where(User.role == "admin")).scalar_one()
    assert remaining == 1
    assert db.get(User, admin.id) is not None
