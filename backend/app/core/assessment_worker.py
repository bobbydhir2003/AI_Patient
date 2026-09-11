"""Database-backed assessment worker pool (B7).

Assessment execution makes several OpenAI calls and takes 30-120s, so it must
NOT run inside the web request. Students enqueue a run (status PENDING) and get
an immediate response; a small pool of background threads claims queued runs and
executes them, so live interviews keep OpenAI/CPU priority.

Durability: the queue is the `assessment_runs` table. A process restart does not
lose work - PENDING runs are still there and get picked up when the worker
restarts (and a run stuck in PROCESSING from a crash is re-reaped after a
timeout). This is intentionally NOT FastAPI BackgroundTasks (which would lose
in-flight jobs on restart) and needs no Redis/Celery for a single service.

Concurrency is ASSESSMENT_WORKER_CONCURRENCY threads PER PROCESS, but the
EFFECTIVE claim rate is capped FLEET-WIDE via a DistributedSemaphore (Redis):
with N uvicorn workers each running their own worker pool, claiming would
otherwise multiply to N x ASSESSMENT_WORKER_CONCURRENCY, which could flood
OpenAI when many students finish around the same time. The DB-backed queue
itself (atomic claim below) is already safe with multiple processes; only the
IN-FLIGHT CAP needed to become fleet-wide. Because live interview generation
has its own (larger, separately-limited) concurrency guard, an assessment
spike still cannot starve interviews of the shared OpenAI budget.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.distributed_semaphore import DistributedSemaphore
from app.core.logging import get_logger
from app.core.telemetry import get_telemetry

logger = get_logger(__name__)

# A run stuck in PROCESSING longer than this (e.g. a worker crashed mid-job) is
# reaped back to PENDING so it can be retried.
_STUCK_PROCESSING_SECONDS = 600

# Fleet-wide cap on concurrently EXECUTING assessment jobs (separate from the
# OpenAI interview and TTS semaphores - assessments must never borrow their
# budget). Falls back to a per-process semaphore in development/test.
_assessment_sem = DistributedSemaphore("assessment")


class AssessmentWorker:
    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._claim_lock = threading.Lock()  # serializes the throttle-check + claim
        self._tokens: dict[str, str] = {}    # run.id -> distributed-semaphore release token
        self._tokens_lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            settings = get_settings()
            self._stop.clear()
            n = max(1, settings.assessment_worker_concurrency)
            for i in range(n):
                t = threading.Thread(target=self._loop, name=f"assessment-worker-{i}", daemon=True)
                t.start()
                self._threads.append(t)
            self._started = True
            logger.info("assessment_worker_started workers=%d", n)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._stop.set()
            self._threads = []
            self._started = False

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        poll = get_settings().assessment_poll_interval_seconds
        while not self._stop.is_set():
            claimed = False
            try:
                claimed = self._process_once()
            except Exception as exc:  # never let a worker thread die
                logger.warning("assessment_worker_error error=%s", exc)
            if not claimed:
                if self._stop.wait(poll):
                    break

    def _process_once(self) -> bool:
        """One poll cycle, split so a PostgreSQL connection is NEVER held while
        an assessment waits on OpenAI (this is what makes high assessment
        concurrency safe against a small DB pool):

          A. CLAIM / LOAD - open a SHORT session, reap stuck jobs, atomically
             claim one PENDING run and commit it to PROCESSING, then CLOSE that
             session completely BEFORE any OpenAI work begins.
          B/C/D. COMPUTE + SAVE + FAILURE - open a SEPARATE, fresh session for
             the pipeline (generation + verification + persistence, and the
             FAILED path). Every OpenAI call in the pipeline is preceded by a
             commit (see usage_recorder.record_openai_usage / the VERIFYING
             transition), and the session uses expire_on_commit=False, so no
             pool connection is checked out across the long provider round trips
             - it is held only for the brief DB writes.

        Returns True iff a run was claimed and executed this cycle (so the loop
        polls again immediately instead of sleeping)."""
        from app.database.connection import get_session_factory
        from app.models import AssessmentRun

        # --- A. CLAIM / LOAD (short-lived session, closed before compute) ---
        run_id: str | None = None
        db = get_session_factory()()
        try:
            self._reap_stuck(db)
            claimed = self._claim_if_capacity(db)
            run_id = claimed.id if claimed is not None else None
        finally:
            db.close()  # released BEFORE any OpenAI work — the whole point
        if run_id is None:
            return False

        # --- B/C/D. COMPUTE + SAVE (dedicated fresh session) ---
        db = get_session_factory()()
        try:
            run = db.get(AssessmentRun, run_id)
            if run is None:
                # Claimed row vanished (should not happen: the claim committed
                # PROCESSING). Release the reserved slot so it can't leak.
                self._release_execution_slot(run_id)
                return True
            self._execute(db, run)
        finally:
            db.close()
        return True

    def _claim_if_capacity(self, db):
        """Adaptive throttle (Part 6): only claim a job if the CURRENT OpenAI
        capacity state allows another concurrent assessment. Live interviews keep
        priority, so assessments back off (fewer effective workers) as OpenAI
        approaches its limits, and stop claiming entirely when CRITICAL+paused.
        Workers never die - they just poll without claiming until capacity returns.

        The in-flight cap is enforced FLEET-WIDE via a DistributedSemaphore
        (reserved atomically before the claim, released in `_execute`), so
        multiple uvicorn workers cannot each independently run up to
        `effective` assessments (which would multiply the real cap by the
        worker count - the same bug this whole change fixes for OpenAI/TTS)."""
        from app.core import capacity

        with self._claim_lock:
            effective, _mode = capacity.effective_assessment_workers()
            if effective <= 0:
                return None
            if get_telemetry().assessment_in_flight.value >= effective:
                return None  # cheap process-local pre-check before touching Redis
            token = _assessment_sem.acquire(effective, 0.0)  # non-blocking: the poll loop retries
            if token is None:
                return None  # fleet-wide cap reached (or Redis required-and-down)
            claimed = self._claim_one(db)
            if claimed is None:
                _assessment_sem.release(token)  # no PENDING row after all; give the slot straight back
                return None
            with self._tokens_lock:
                self._tokens[claimed.id] = token
            get_telemetry().assessment_in_flight.inc()  # reserve the slot now
            return claimed

    def _reap_stuck(self, db) -> None:
        from app.models import AssessmentRun

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_STUCK_PROCESSING_SECONDS)
        res = db.execute(
            update(AssessmentRun)
            .where(AssessmentRun.status == "PROCESSING", AssessmentRun.created_at < cutoff)
            .values(status="PENDING")
        )
        if res.rowcount:
            db.commit()
            logger.warning("assessment_reaped_stuck count=%d", res.rowcount)
        else:
            db.rollback()

    def _claim_one(self, db):
        """Atomically claim the oldest PENDING run (SELECT then conditional
        UPDATE). Only one worker's UPDATE can flip a given row, so no two workers
        run the same job."""
        from app.models import AssessmentRun

        row = db.execute(
            select(AssessmentRun.id).where(AssessmentRun.status == "PENDING")
            .order_by(AssessmentRun.created_at).limit(1)
        ).first()
        if not row:
            return None
        run_id = row[0]
        res = db.execute(
            update(AssessmentRun)
            .where(AssessmentRun.id == run_id, AssessmentRun.status == "PENDING")
            .values(status="PROCESSING")
        )
        db.commit()
        if res.rowcount == 1:
            return db.get(AssessmentRun, run_id)
        return None  # another worker grabbed it first

    def _execute(self, db, run) -> None:
        """Run the pipeline for a claimed run on the given (fresh) session.

        The in-flight slot + fleet-wide semaphore token were RESERVED at claim
        time (_claim_if_capacity); this method releases them when done. Does NOT
        close `db` - the caller (_process_once) owns that session's lifecycle."""
        tele = get_telemetry()
        settings = get_settings()
        t0 = time.monotonic()
        run_id = run.id  # capture before any commit/detach so the finally is safe
        try:
            if settings.mock_ai:
                self._execute_mock(db, run)
            else:
                from app.assessment import assessment_service
                from app.patient_engine.openai_client import get_openai_client

                assessment_service.execute_run(db, run, get_openai_client())
            tele.openai.window.incr("assessment_completed")
        except Exception as exc:
            # The pipeline marks the run FAILED itself; log and continue.
            logger.warning("assessment_job_failed run=%s error=%s", run_id, exc)
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._release_execution_slot(run_id)
            tele.openai.window.observe_latency(duration_ms)  # coarse job duration signal

    def _release_execution_slot(self, run_id: str) -> None:
        """Release the fleet-wide assessment semaphore token + the process-local
        in-flight gauge reserved for this run at claim time. Safe if no token
        was stored (DistributedSemaphore.release(None) is a no-op), so the
        vanished-row path and the direct-_execute test path both stay correct."""
        get_telemetry().assessment_in_flight.dec()
        with self._tokens_lock:
            token = self._tokens.pop(run_id, None)
        _assessment_sem.release(token)

    def _execute_mock(self, db, run) -> None:
        """MOCK_AI: simulate assessment work without spending, so the queue and
        worker concurrency can be load-tested."""
        from app.models import AssessmentRun

        time.sleep(get_settings().mock_model_latency_ms / 1000.0)
        db.execute(
            update(AssessmentRun).where(AssessmentRun.id == run.id).values(
                status="COMPLETE", verification_status="VERIFIED",
                overall_level="Proficient", overall_summary="(mock assessment)",
                completed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()


_worker: AssessmentWorker | None = None


def get_assessment_worker() -> AssessmentWorker:
    global _worker
    if _worker is None:
        _worker = AssessmentWorker()
    return _worker
