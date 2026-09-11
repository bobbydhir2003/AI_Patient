"""Focused proofs for the assessment-worker DB-connection-lifetime refactor.

The refactor (core/assessment_worker._process_once) splits each poll cycle into:
  A. CLAIM/LOAD  - short session, claim -> PROCESSING, CLOSED before compute
  B/C/D. COMPUTE + SAVE + FAILURE - a SEPARATE fresh session for the pipeline

These tests prove the worker never carries the claim/poll session into the
OpenAI/compute phase, saves on a fresh session, releases its fleet-wide slot on
both success and failure, and that the adaptive-capacity tiers are 20/12/6/0.

They deliberately fake `assessment_service.execute_run` (the scoring pipeline is
covered elsewhere) so the proof is about SESSION LIFETIME, not scoring.
"""
from sqlalchemy.orm import sessionmaker

import app.assessment.assessment_service as assessment_service
from app.assessment.assessment_repository import AssessmentRepository
from app.core import assessment_worker as worker_module
from app.core.assessment_worker import AssessmentWorker
from app.core.distributed_semaphore import DistributedSemaphore
from app.core.telemetry import get_telemetry, reset_telemetry
from app.models import AssessmentRun


def _tracking_factory(engine):
    """A get_session_factory() stand-in bound to the test engine that records
    every session it hands out and whether/when each was closed."""
    base = sessionmaker(bind=engine, expire_on_commit=False)
    created: list[dict] = []

    def make_session():
        s = base()
        rec = {"session": s, "closed": False}
        orig_close = s.close

        def _close():
            rec["closed"] = True
            orig_close()

        s.close = _close
        created.append(rec)
        return s

    # get_session_factory() returns a *callable* that makes a session.
    return (lambda: make_session), created, base


def _seed_pending_run(base, *, session_id="dbl"):
    db = base()
    try:
        run = AssessmentRepository(db).create_run(
            session_id=session_id, case_id="carly", status="PENDING"
        )
        db.commit()
        return run.id
    finally:
        db.close()


def test_claim_session_closed_before_compute_and_saved_on_fresh_session(engine, monkeypatch):
    reset_telemetry()
    # Isolated fleet-wide semaphore so this test's acquire/release can't leak
    # into the module singleton other tests share.
    monkeypatch.setattr(worker_module, "_assessment_sem", DistributedSemaphore("test_dbl_ok"))

    factory, created, base = _tracking_factory(engine)
    monkeypatch.setattr("app.database.connection.get_session_factory", factory)
    run_id = _seed_pending_run(base)

    observed = {}

    def fake_execute_run(db, run, client):
        # This runs at the START of the compute/OpenAI phase.
        # created[0] = claim session, created[1] = compute session.
        observed["claim_closed_before_compute"] = created[0]["closed"]
        observed["compute_is_separate_session"] = db is created[1]["session"]
        observed["compute_not_claim_session"] = db is not created[0]["session"]
        # Save the result on THIS fresh session (proves the save phase session).
        run.status = "COMPLETE"
        run.verification_status = "VERIFIED"
        db.commit()

    monkeypatch.setattr(assessment_service, "execute_run", fake_execute_run)

    worker = AssessmentWorker()
    did = worker._process_once()

    assert did is True
    assert observed["claim_closed_before_compute"] is True
    assert observed["compute_is_separate_session"] is True
    assert observed["compute_not_claim_session"] is True
    # Exactly two sessions were opened by the cycle: claim + compute.
    assert len(created) == 2
    assert created[0]["closed"] is True and created[1]["closed"] is True

    # Result persisted (on the fresh compute session).
    check = base()
    try:
        row = check.get(AssessmentRun, run_id)
        assert row.status == "COMPLETE"
    finally:
        check.close()

    # Fleet-wide slot released (no leak) after success.
    assert worker_module._assessment_sem.acquire(1, 0.0) is not None


def test_failure_marks_status_on_fresh_session_and_releases_slot(engine, monkeypatch):
    reset_telemetry()
    monkeypatch.setattr(worker_module, "_assessment_sem", DistributedSemaphore("test_dbl_fail"))

    factory, created, base = _tracking_factory(engine)
    monkeypatch.setattr("app.database.connection.get_session_factory", factory)
    run_id = _seed_pending_run(base, session_id="dbl-fail")

    observed = {}

    def failing_execute_run(db, run, client):
        # Mirror the real pipeline's failure contract: mark FAILED on the fresh
        # (compute) session, commit, then raise AssessmentUnavailableError.
        observed["claim_closed_before_compute"] = created[0]["closed"]
        observed["compute_is_separate_session"] = db is not created[0]["session"]
        run.status = "FAILED"
        run.error_code = "ASSESSMENT_UNAVAILABLE"
        db.commit()
        raise RuntimeError("simulated pipeline failure after marking FAILED")

    monkeypatch.setattr(assessment_service, "execute_run", failing_execute_run)

    worker = AssessmentWorker()
    # _execute swallows the pipeline exception (logs it); _process_once returns True.
    did = worker._process_once()

    assert did is True
    assert observed["claim_closed_before_compute"] is True
    assert observed["compute_is_separate_session"] is True

    check = base()
    try:
        row = check.get(AssessmentRun, run_id)
        assert row.status == "FAILED"  # written via the fresh compute session
    finally:
        check.close()

    # Slot released even on failure (no leak).
    assert worker_module._assessment_sem.acquire(1, 0.0) is not None
    # In-flight gauge returned to 0 (reserved at claim, released in _execute).
    assert get_telemetry().assessment_in_flight.value <= 0


def test_no_pending_run_is_a_noop_cycle(engine, monkeypatch):
    reset_telemetry()
    monkeypatch.setattr(worker_module, "_assessment_sem", DistributedSemaphore("test_dbl_empty"))
    factory, created, base = _tracking_factory(engine)
    monkeypatch.setattr("app.database.connection.get_session_factory", factory)
    # No PENDING run seeded.
    worker = AssessmentWorker()
    did = worker._process_once()
    assert did is False
    # Only the claim session was opened, and it was closed.
    assert len(created) == 1 and created[0]["closed"] is True


def test_effective_assessment_workers_tiers_are_20_12_6_0(monkeypatch):
    from app.core import capacity
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "assessment_worker_concurrency", 20)
    monkeypatch.setattr(s, "assessment_workers_busy", 12)
    monkeypatch.setattr(s, "assessment_workers_protecting", 6)
    monkeypatch.setattr(s, "assessment_pause_on_critical", True)

    assert capacity.effective_assessment_workers("NORMAL") == (20, "NORMAL")
    assert capacity.effective_assessment_workers("BUSY") == (12, "REDUCED")
    assert capacity.effective_assessment_workers("PROTECTING") == (6, "MINIMAL")
    assert capacity.effective_assessment_workers("CRITICAL") == (0, "PAUSED")


def test_global_semaphore_caps_execution_fleet_wide(monkeypatch):
    """The fleet-wide assessment semaphore admits at most `capacity` tokens even
    though every uvicorn process runs its own thread pool - this is what keeps
    3 processes x 20 threads from executing 60 assessments at once."""
    sem = DistributedSemaphore("test_dbl_cap")
    tokens = [sem.acquire(20, 0.0) for _ in range(20)]
    assert all(t is not None for t in tokens)          # 20 admitted
    assert sem.acquire(20, 0.0) is None                # the 21st is refused
    sem.release(tokens[0])
    assert sem.acquire(20, 0.0) is not None             # a freed slot re-admits
