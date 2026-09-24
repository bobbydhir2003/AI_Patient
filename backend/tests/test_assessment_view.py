"""Active assessment viewing-time tracker (silent, admin-only).

Visit-based semantics: a VISIT is one real page mount (client visit_id). views =
number of visit rows; active time is credited server-side per visit and a new
visit's first ping credits 0 (away/between-visit gaps never count).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.constants import SESSION_STATUS_ACTIVE, SESSION_STATUS_COMPLETED
from app.models import (
    AssessmentRun,
    AssessmentViewVisit,
    InterviewSession,
    Student,
    User,
)
from app.services import admin_service
from app.services import assessment_view_service as avs
from tests.conftest import make_client

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed(db, *, email, number, status=SESSION_STATUS_COMPLETED, case="camden"):
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


def _visits(db, session_id):
    return list(
        db.execute(
            select(AssessmentViewVisit).where(
                AssessmentViewVisit.interview_session_id == session_id
            )
        ).scalars().all()
    )


# ---- 1. first unique visit: view_count=1, active_seconds=0 -------------------
def test_first_visit_creates_row_and_credits_zero(db_session):
    user, _s, session, run = _seed(db_session, email="a@s.edu", number="A1")
    summary = avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    assert summary is not None
    assert summary.view_count == 1
    assert summary.active_seconds == 0
    # SQLite (test DB) returns naive datetimes; normalize to UTC for comparison.
    assert avs._as_utc(summary.first_viewed_at) == T0
    assert avs._as_utc(summary.last_viewed_at) == T0
    visits = _visits(db_session, session.id)
    assert len(visits) == 1 and visits[0].active_seconds == 0


# ---- 2. same visit heartbeat credits interval, no new view -------------------
def test_same_visit_heartbeat_credits_and_keeps_one_view(db_session):
    user, _s, session, run = _seed(db_session, email="b@s.edu", number="B1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=20))
    assert s.active_seconds == 20
    assert s.view_count == 1
    assert _visits(db_session, session.id)[0].active_seconds == 20


# ---- 3/4/5/6. new visit_id => view+1, first ping credits 0 (no away gap) -----
def test_new_visit_immediately_after_first(db_session):
    user, _s, _sess, run = _seed(db_session, email="c@s.edu", number="C1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v2", now=T0 + timedelta(seconds=1))
    assert s.view_count == 2
    assert s.active_seconds == 0  # new visit credits 0; the 1s away is not counted


def test_new_visit_after_short_medium_long_gaps_never_credits_gap(db_session):
    for gap in (10, 60, 300):
        user, _s, _sess, run = _seed(db_session, email=f"g{gap}@s.edu", number=f"G{gap}")
        avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
        avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=20))
        s = avs.record_ping(
            db_session, run=run, user=user, visit_id="v2",
            now=T0 + timedelta(seconds=20 + gap),
        )
        assert s.view_count == 2, gap
        assert s.active_seconds == 20, gap  # only the in-visit 20s; gap not credited


# ---- 7. repeated same visitId / retry does not duplicate the visit ----------
def test_repeated_same_visit_id_is_idempotent(db_session):
    user, _s, session, run = _seed(db_session, email="d@s.edu", number="D1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)  # retry
    assert s.view_count == 1
    assert len(_visits(db_session, session.id)) == 1


# ---- 8/9. delta clamping within a visit ------------------------------------
def test_delta_over_max_credit_credits_zero(db_session):
    user, _s, _sess, run = _seed(db_session, email="e@s.edu", number="E1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=60))
    assert s.active_seconds == 0  # 60 > MAX_CREDIT(45)
    assert s.view_count == 1


def test_negative_delta_credits_zero(db_session):
    user, _s, _sess, run = _seed(db_session, email="f@s.edu", number="F1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=20))
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)  # backwards
    assert s.active_seconds == 0


# ---- active-timer events: pause banks the tail, resume never credits the gap --
def _ping(db, run, user, vid, secs, event=None):
    return avs.record_ping(
        db, run=run, user=user, visit_id=vid, event=event, now=T0 + timedelta(seconds=secs)
    )


def test_short_visit_pause_banks_full_tail(db_session):
    # 5s / 10s / 35s visits must record ~5 / ~10 / ~35, not 0 / 0 / 20.
    for tail, expected in ((5, 5), (10, 10)):
        user, _s, _sess, run = _seed(db_session, email=f"p{tail}@s.edu", number=f"P{tail}")
        _ping(db_session, run, user, "v1", 0, "resume")           # start (new visit, 0)
        s = _ping(db_session, run, user, "v1", tail, "pause")     # leave -> bank tail
        assert s.active_seconds == expected, tail
        assert s.view_count == 1, tail

    user, _s, _sess, run = _seed(db_session, email="p35@s.edu", number="P35")
    _ping(db_session, run, user, "v1", 0, "resume")
    _ping(db_session, run, user, "v1", 20, "heartbeat")           # +20
    s = _ping(db_session, run, user, "v1", 35, "pause")           # +15 tail
    assert s.active_seconds == 35
    assert s.view_count == 1


def test_hidden_gap_pause_resume_excludes_background_time(db_session):
    # visible 12s -> hidden 5min -> visible 10s => ~22s, hidden counted 0.
    user, _s, _sess, run = _seed(db_session, email="hg@s.edu", number="HG1")
    _ping(db_session, run, user, "v1", 0, "resume")
    _ping(db_session, run, user, "v1", 12, "pause")              # bank 12
    _ping(db_session, run, user, "v1", 12 + 300, "resume")       # back after 5min -> 0
    s = _ping(db_session, run, user, "v1", 12 + 300 + 10, "pause")  # bank 10
    assert s.active_seconds == 22
    assert s.view_count == 1  # same visit throughout; pause/resume never add views


def test_resume_never_credits_gap(db_session):
    user, _s, _sess, run = _seed(db_session, email="rz@s.edu", number="RZ1")
    _ping(db_session, run, user, "v1", 0, "resume")
    s = _ping(db_session, run, user, "v1", 5, "resume")          # a resume always credits 0
    assert s.active_seconds == 0


def test_duplicate_pause_does_not_double_credit(db_session):
    # The visibilitychange + pagehide + unmount burst can send several pauses.
    user, _s, _sess, run = _seed(db_session, email="dp@s.edu", number="DP1")
    _ping(db_session, run, user, "v1", 0, "resume")
    _ping(db_session, run, user, "v1", 8, "pause")              # bank 8
    _ping(db_session, run, user, "v1", 8, "pause")              # ~0
    s = _ping(db_session, run, user, "v1", 8, "pause")          # ~0
    assert s.active_seconds == 8


def test_transcript_toggle_pauses_and_resumes_same_visit(db_session):
    # Assessment 8s -> Transcript (pause) -> 60s (0) -> Assessment (resume) -> 10s.
    user, _s, _sess, run = _seed(db_session, email="tt@s.edu", number="TT1")
    _ping(db_session, run, user, "v1", 0, "resume")
    _ping(db_session, run, user, "v1", 8, "pause")             # bank 8
    _ping(db_session, run, user, "v1", 8 + 60, "resume")       # back from transcript -> 0
    s = _ping(db_session, run, user, "v1", 8 + 60 + 10, "pause")  # bank 10
    assert s.active_seconds == 18
    assert s.view_count == 1


def test_missing_event_defaults_to_heartbeat_backward_compatible(db_session):
    # An old client that omits event behaves exactly like the pre-event heartbeat.
    user, _s, _sess, run = _seed(db_session, email="bc@s.edu", number="BC1")
    _ping(db_session, run, user, "v1", 0)                      # no event -> heartbeat
    s = _ping(db_session, run, user, "v1", 20)                 # no event -> +20
    assert s.active_seconds == 20
    assert s.view_count == 1


# ---- 10/11/12. summary aggregates over visits ------------------------------
def test_summary_active_equals_sum_of_visit_active(db_session):
    user, _s, session, run = _seed(db_session, email="h@s.edu", number="H1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=20))  # +20
    avs.record_ping(db_session, run=run, user=user, visit_id="v2", now=T0 + timedelta(seconds=200))  # new, 0
    s = avs.record_ping(db_session, run=run, user=user, visit_id="v2", now=T0 + timedelta(seconds=220))  # +20
    assert s.active_seconds == 40
    assert sum(v.active_seconds for v in _visits(db_session, session.id)) == 40
    assert s.view_count == 2
    assert avs._as_utc(s.first_viewed_at) == T0  # first visit start preserved
    assert avs._as_utc(s.last_viewed_at) == T0 + timedelta(seconds=220)  # latest heartbeat


# ---- ownership / completed gates -------------------------------------------
def test_non_owner_user_credits_nothing(db_session):
    user, _s, session, run = _seed(db_session, email="owner@s.edu", number="O1")
    other, *_ = _seed(db_session, email="other@s.edu", number="X1")
    assert avs.record_ping(db_session, run=run, user=other, visit_id="v1", now=T0) is None
    assert avs.get_for_session(db_session, session.id) is None
    assert _visits(db_session, session.id) == []


def test_incomplete_session_not_tracked(db_session):
    user, _s, session, run = _seed(
        db_session, email="i@s.edu", number="I1", status=SESSION_STATUS_ACTIVE
    )
    assert avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0) is None
    assert avs.get_for_session(db_session, session.id) is None


# ---- backward compat: missing visit_id => one implicit legacy visit ---------
def test_missing_visit_id_maps_to_single_legacy_visit(db_session):
    user, _s, session, run = _seed(db_session, email="j@s.edu", number="J1")
    avs.record_ping(db_session, run=run, user=user, visit_id=None, now=T0)
    s = avs.record_ping(db_session, run=run, user=user, visit_id=None, now=T0 + timedelta(seconds=20))
    assert s.view_count == 1  # never fabricates repeated legacy visits
    visits = _visits(db_session, session.id)
    assert len(visits) == 1
    assert visits[0].visit_id == avs.LEGACY_CLIENT_VISIT_ID


# ---- 13. map_for_sessions exposes last_viewed_at ---------------------------
def test_map_for_sessions_returns_last_viewed_at(db_session):
    user, _s, session, run = _seed(db_session, email="k@s.edu", number="K1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    m = avs.map_for_sessions(db_session, [session.id])
    assert avs._as_utc(m[session.id].last_viewed_at) == T0


# ---- admin list attaches fields via batched map ----------------------------
def test_list_sessions_with_view_time_batched(db_session):
    user_a, _sa, session_a, run_a = _seed(db_session, email="la@s.edu", number="LA1")
    _ub, _sb, session_b, _rb = _seed(db_session, email="lb@s.edu", number="LB1")
    avs.record_ping(db_session, run=run_a, user=user_a, visit_id="v1", now=T0)
    avs.record_ping(db_session, run=run_a, user=user_a, visit_id="v1", now=T0 + timedelta(seconds=20))

    page = admin_service.list_sessions(db_session, with_view_time=True, page_size=100)
    by_id = {i.session_id: i for i in page.items}
    assert by_id[session_a.id].active_viewing_seconds == 20
    assert by_id[session_a.id].view_count == 1
    assert by_id[session_a.id].first_viewed_at is not None
    assert by_id[session_a.id].last_viewed_at is not None
    # Never-viewed session: all fields None.
    assert by_id[session_b.id].active_viewing_seconds is None
    assert by_id[session_b.id].view_count is None
    assert by_id[session_b.id].last_viewed_at is None


def test_list_sessions_without_flag_leaves_fields_none(db_session):
    user, _s, session, run = _seed(db_session, email="lc@s.edu", number="LC1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    page = admin_service.list_sessions(db_session, page_size=100)  # default False
    item = next(i for i in page.items if i.session_id == session.id)
    assert item.active_viewing_seconds is None
    assert item.last_viewed_at is None


# ---- endpoint: ownership + 204 ---------------------------------------------
def _default_student_id(engine) -> str:
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        return db.execute(
            select(User.student_id).where(User.email == "default@school.edu")
        ).scalar_one()
    finally:
        db.close()


def test_ping_endpoint_owner_204_and_records_visit(engine, fake_client):
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

        r = c.post(f"/api/assessments/{run_id}/view/ping", json={"visitId": "v1"})
        assert r.status_code == 204

        check = factory()
        row = avs.get_for_session(check, session_id)
        visits = _visits(check, session_id)
        check.close()
        assert row is not None and row.view_count == 1 and row.active_seconds == 0
        assert len(visits) == 1 and visits[0].visit_id == "v1"


def test_ping_endpoint_wrong_student_404(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        db = factory()
        _u, _s, session, run = _seed(db, email="someoneelse@s.edu", number="Z9")
        run_id, session_id = run.id, session.id
        db.close()

        r = c.post(f"/api/assessments/{run_id}/view/ping", json={"visitId": "v1"})
        assert r.status_code == 404

        check = factory()
        assert avs.get_for_session(check, session_id) is None
        check.close()


# ---- 14. deletion removes visits + summary ---------------------------------
def test_delete_for_sessions_removes_visits_and_summary(db_session):
    user, _s, session, run = _seed(db_session, email="m@s.edu", number="M1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    avs.record_ping(db_session, run=run, user=user, visit_id="v2", now=T0 + timedelta(seconds=200))
    assert avs.get_for_session(db_session, session.id) is not None
    assert len(_visits(db_session, session.id)) == 2

    avs.delete_for_sessions(db_session, [session.id])
    db_session.commit()
    assert avs.get_for_session(db_session, session.id) is None
    assert _visits(db_session, session.id) == []


def test_delete_session_path_removes_visits_and_summary(db_session):
    admin = User(
        email="admin@s.edu", password_hash="x", full_name="Admin", student_number="",
        role="admin", student_id=None, is_active=True,
    )
    db_session.add(admin)
    db_session.flush()
    user, _s, session, run = _seed(db_session, email="n@s.edu", number="N1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    assert len(_visits(db_session, session.id)) == 1

    admin_service.delete_session(db_session, admin, session.id)
    assert avs.get_for_session(db_session, session.id) is None
    assert _visits(db_session, session.id) == []


# ---- visit source ------------------------------------------------------------
def test_visit_source_initial_and_dashboard_are_stored(db_session):
    user, _s, session, run = _seed(db_session, email="src@s.edu", number="SRC1")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", source="initial_assessment", now=T0)
    avs.record_ping(
        db_session, run=run, user=user, visit_id="v2", source="student_dashboard",
        now=T0 + timedelta(minutes=5),
    )
    by_id = {v.visit_id: v.source for v in _visits(db_session, session.id)}
    assert by_id == {"v1": "initial_assessment", "v2": "student_dashboard"}


def test_visit_source_is_never_overwritten_by_later_pings(db_session):
    user, _s, session, run = _seed(db_session, email="src2@s.edu", number="SRC2")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", source="initial_assessment", now=T0)
    # Same visit, later heartbeats claiming another (or no) source: ignored.
    avs.record_ping(
        db_session, run=run, user=user, visit_id="v1", source="student_dashboard",
        now=T0 + timedelta(seconds=20),
    )
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0 + timedelta(seconds=40))
    [visit] = _visits(db_session, session.id)
    assert visit.source == "initial_assessment"
    assert visit.active_seconds == 40


def test_missing_or_unknown_source_is_stored_as_unknown(db_session):
    user, _s, session, run = _seed(db_session, email="src3@s.edu", number="SRC3")
    avs.record_ping(db_session, run=run, user=user, visit_id="v1", now=T0)
    avs.record_ping(db_session, run=run, user=user, visit_id="v2", source="legacy", now=T0)
    assert {v.source for v in _visits(db_session, session.id)} == {"unknown"}


def test_ping_endpoint_records_source_and_rejects_invalid_enum(engine, fake_client):
    with make_client(engine, fake_client, authenticate=True) as c:
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        db = factory()
        session = InterviewSession(
            student_id=_default_student_id(engine), case_id="camden",
            status=SESSION_STATUS_COMPLETED, locked=True,
        )
        db.add(session)
        db.flush()
        run = AssessmentRun(session_id=session.id, case_id="camden", status="COMPLETE")
        db.add(run)
        db.commit()
        run_id, session_id = run.id, session.id
        db.close()

        ok = c.post(
            f"/api/assessments/{run_id}/view/ping",
            json={"visitId": "v1", "source": "student_dashboard"},
        )
        assert ok.status_code == 204
        # Client may only choose from the enum; "legacy" is server-only.
        bad = c.post(
            f"/api/assessments/{run_id}/view/ping", json={"visitId": "v2", "source": "legacy"}
        )
        assert bad.status_code == 422

        check = factory()
        visits = _visits(check, session_id)
        check.close()
        assert [(v.visit_id, v.source) for v in visits] == [("v1", "student_dashboard")]


def test_migration_0025_backfills_one_legacy_visit(tmp_path, monkeypatch):
    """0025 on a 0024-shaped DB: each summary row gets ONE visit with
    source='legacy' and identical active time; 0026 then adds the reset table.
    Runs against a throwaway SQLite file only."""
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from app.core.config import get_settings
    from tests.test_survey_migration import _alembic_config

    url = f"sqlite:///{tmp_path / 'visits.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE students (id VARCHAR(32) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE interview_sessions (id VARCHAR(32) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE assessment_runs (id VARCHAR(32) PRIMARY KEY)"))
            conn.execute(text(
                "CREATE TABLE assessment_view_sessions (id VARCHAR(32) PRIMARY KEY, "
                "interview_session_id VARCHAR(32), assessment_run_id VARCHAR(32), "
                "user_id VARCHAR(32), student_id VARCHAR(32), case_id VARCHAR(50), "
                "active_seconds INTEGER, view_count INTEGER, first_viewed_at DATETIME, "
                "last_heartbeat_at DATETIME, created_at DATETIME, updated_at DATETIME)"
            ))
            # Real parent rows: some suites enable SQLite FK enforcement globally.
            conn.execute(text("INSERT INTO users (id) VALUES ('u1')"))
            conn.execute(text("INSERT INTO interview_sessions (id) VALUES ('s1')"))
            conn.execute(text("INSERT INTO assessment_runs (id) VALUES ('r1')"))
            conn.execute(text(
                "INSERT INTO assessment_view_sessions VALUES ('a1','s1','r1','u1','st1',"
                "'camden',95,4,'2026-01-01 10:00:00','2026-01-01 11:00:00',"
                "'2026-01-01 10:00:00','2026-01-01 11:00:00')"
            ))
        eng.dispose()

        config = _alembic_config(url)
        command.stamp(config, "0024")
        command.upgrade(config, "head")

        eng = create_engine(url)
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT visit_id, source, active_seconds FROM assessment_view_visits"
            )).all()
            assert rows == [("legacy", "legacy", 95)]
            assert "survey_receipt_resets" in inspect(conn).get_table_names()
        eng.dispose()

        command.downgrade(config, "0024")
        eng = create_engine(url)
        with eng.connect() as conn:
            names = inspect(conn).get_table_names()
        eng.dispose()
        assert "assessment_view_visits" not in names
        assert "survey_receipt_resets" not in names
    finally:
        get_settings.cache_clear()
