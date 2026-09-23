"""Active assessment viewing-time tracker (silent, admin-only).

Server-authoritative crediting is verified with an injected clock; ownership and
deletion are verified through the real endpoint / delete path.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.constants import SESSION_STATUS_ACTIVE, SESSION_STATUS_COMPLETED
from app.models import AssessmentRun, InterviewSession, Student, User
from app.services import admin_service
from app.services import assessment_view_service as avs
from tests.conftest import make_client

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed(db, *, email, number, status=SESSION_STATUS_COMPLETED, case="camden"):
    """Create student + linked user + a session (completed by default) + run."""
    student = Student(name=email, student_number=number, email=email)
    db.add(student)
    db.flush()
    user = User(
        email=email, password_hash="x", full_name=email, student_number=number,
        role="student", student_id=student.id, is_active=True,
    )
    db.add(user)
    session = InterviewSession(
        student_id=student.id, case_id=case, status=status,
        locked=(status == SESSION_STATUS_COMPLETED),
    )
    db.add(session)
    db.flush()
    run = AssessmentRun(session_id=session.id, case_id=case, status="COMPLETE")
    db.add(run)
    db.commit()
    return user, student, session, run


# -------------------------------------------------------------- crediting logic
def test_first_ping_creates_row_and_credits_zero(db_session):
    user, _s, session, run = _seed(db_session, email="a@s.edu", number="A1")
    row = avs.record_ping(db_session, run=run, user=user, now=T0)
    assert row is not None
    assert row.active_seconds == 0
    assert row.view_count == 1
    assert row.interview_session_id == session.id
    assert row.student_id == session.student_id
    assert row.case_id == "camden"


def test_second_ping_credits_normal_interval(db_session):
    user, _s, _sess, run = _seed(db_session, email="b@s.edu", number="B1")
    avs.record_ping(db_session, run=run, user=user, now=T0)
    row = avs.record_ping(db_session, run=run, user=user, now=T0 + timedelta(seconds=20))
    assert row.active_seconds == 20
    assert row.view_count == 1


def test_long_gap_credits_zero_and_starts_new_view(db_session):
    user, _s, _sess, run = _seed(db_session, email="c@s.edu", number="C1")
    avs.record_ping(db_session, run=run, user=user, now=T0)
    row = avs.record_ping(db_session, run=run, user=user, now=T0 + timedelta(seconds=300))
    assert row.active_seconds == 0  # away 5 min -> nothing credited
    assert row.view_count == 2      # counted as a new viewing session


def test_gap_over_max_credit_but_under_reset_credits_zero_same_view(db_session):
    user, _s, _sess, run = _seed(db_session, email="d@s.edu", number="D1")
    avs.record_ping(db_session, run=run, user=user, now=T0)
    row = avs.record_ping(db_session, run=run, user=user, now=T0 + timedelta(seconds=60))
    assert row.active_seconds == 0  # 60s > MAX_CREDIT(45) -> no credit
    assert row.view_count == 1      # 60s < GAP_RESET(120) -> not a new view


def test_negative_delta_credits_zero(db_session):
    user, _s, _sess, run = _seed(db_session, email="e@s.edu", number="E1")
    avs.record_ping(db_session, run=run, user=user, now=T0 + timedelta(seconds=20))
    row = avs.record_ping(db_session, run=run, user=user, now=T0)  # clock goes backwards
    assert row.active_seconds == 0


def test_accumulates_across_several_pings(db_session):
    user, _s, _sess, run = _seed(db_session, email="f@s.edu", number="F1")
    for i in range(4):  # T0, +20, +40, +60 -> credits 0,20,20,20 = 60
        avs.record_ping(db_session, run=run, user=user, now=T0 + timedelta(seconds=20 * i))
    row = avs.get_for_session(db_session, run.session_id)
    assert row.active_seconds == 60


# --------------------------------------------------------------- ownership gate
def test_non_owner_user_credits_nothing(db_session):
    user, _s, session, run = _seed(db_session, email="owner@s.edu", number="O1")
    other, *_ = _seed(db_session, email="other@s.edu", number="X1")
    assert avs.record_ping(db_session, run=run, user=other, now=T0) is None
    assert avs.get_for_session(db_session, session.id) is None


def test_incomplete_session_not_tracked(db_session):
    user, _s, session, run = _seed(
        db_session, email="g@s.edu", number="G1", status=SESSION_STATUS_ACTIVE
    )
    assert avs.record_ping(db_session, run=run, user=user, now=T0) is None
    assert avs.get_for_session(db_session, session.id) is None


# ------------------------------------------------------------------- endpoint
def _default_student_id(engine) -> str:
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        return db.execute(
            select(User.student_id).where(User.email == "default@school.edu")
        ).scalar_one()
    finally:
        db.close()


def test_ping_endpoint_owner_returns_204_and_records(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        db = factory()
        sid = _default_student_id(engine)
        session = InterviewSession(
            student_id=sid, case_id="camden", status=SESSION_STATUS_COMPLETED, locked=True
        )
        db.add(session)
        db.flush()
        run = AssessmentRun(session_id=session.id, case_id="camden", status="COMPLETE")
        db.add(run)
        db.commit()
        run_id, session_id = run.id, session.id
        db.close()

        # Tokenless request is treated as the seeded default (owning) student.
        r = c.post(f"/api/assessments/{run_id}/view/ping")
        assert r.status_code == 204

        check = factory()
        row = avs.get_for_session(check, session_id)
        check.close()
        assert row is not None
        assert row.active_seconds == 0  # first ping credits nothing


def test_ping_endpoint_wrong_student_gets_404(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        # A run owned by a DIFFERENT student; the default student must not reach it.
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        db = factory()
        _u, _s, session, run = _seed(db, email="someoneelse@s.edu", number="Z9")
        run_id, session_id = run.id, session.id
        db.close()

        r = c.post(f"/api/assessments/{run_id}/view/ping")
        assert r.status_code == 404  # require_assessment_access: no existence leak

        check = factory()
        assert avs.get_for_session(check, session_id) is None
        check.close()


# ------------------------------------------------------------------- deletion
def test_delete_for_sessions_removes_row(db_session):
    user, _s, session, run = _seed(db_session, email="h@s.edu", number="H1")
    avs.record_ping(db_session, run=run, user=user, now=T0)
    assert avs.get_for_session(db_session, session.id) is not None
    avs.delete_for_sessions(db_session, [session.id])
    db_session.commit()
    assert avs.get_for_session(db_session, session.id) is None


def test_list_sessions_with_view_time_attaches_fields_batched(db_session):
    # Two completed sessions; only one is viewed. Batched map must fill the viewed
    # one and leave the unviewed one at None (never 0-as-viewed).
    user_a, _sa, session_a, run_a = _seed(db_session, email="la@s.edu", number="LA1")
    user_b, _sb, session_b, _run_b = _seed(db_session, email="lb@s.edu", number="LB1")
    avs.record_ping(db_session, run=run_a, user=user_a, now=T0)
    avs.record_ping(db_session, run=run_a, user=user_a, now=T0 + timedelta(seconds=20))

    page = admin_service.list_sessions(db_session, with_view_time=True, page_size=100)
    by_id = {i.session_id: i for i in page.items}

    assert by_id[session_a.id].active_viewing_seconds == 20
    assert by_id[session_a.id].view_count == 1
    assert by_id[session_a.id].first_viewed_at is not None
    # Unviewed session: fields stay None (rendered as "—" in the admin table).
    assert by_id[session_b.id].active_viewing_seconds is None
    assert by_id[session_b.id].view_count is None
    assert by_id[session_b.id].first_viewed_at is None


def test_list_sessions_without_flag_leaves_view_fields_none(db_session):
    user, _s, session, run = _seed(db_session, email="lc@s.edu", number="LC1")
    avs.record_ping(db_session, run=run, user=user, now=T0)

    page = admin_service.list_sessions(db_session, page_size=100)  # default False
    item = next(i for i in page.items if i.session_id == session.id)
    assert item.active_viewing_seconds is None
    assert item.view_count is None
    assert item.first_viewed_at is None


def test_delete_session_path_removes_view_row(db_session):
    admin = User(
        email="admin@s.edu", password_hash="x", full_name="Admin", student_number="",
        role="admin", student_id=None, is_active=True,
    )
    db_session.add(admin)
    db_session.flush()
    user, _s, session, run = _seed(db_session, email="i@s.edu", number="I1")
    avs.record_ping(db_session, run=run, user=user, now=T0)
    assert avs.get_for_session(db_session, session.id) is not None

    admin_service.delete_session(db_session, admin, session.id)
    assert avs.get_for_session(db_session, session.id) is None
