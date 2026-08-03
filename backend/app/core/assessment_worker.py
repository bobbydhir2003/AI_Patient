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

Concurrency is ASSESSMENT_WORKER_CONCURRENCY threads. Because live interview
generation has its own (larger) concurrency guard, an assessment spike cannot
starve interviews of the shared OpenAI budget.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.telemetry import get_telemetry

logger = get_logger(__name__)

# A run stuck in PROCESSING longer than this (e.g. a worker crashed mid-job) is
# reaped back to PENDING so it can be retried.
_STUCK_PROCESSING_SECONDS = 600


class AssessmentWorker:
    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._claim_lock = threading.Lock()  # serializes the throttle-check + claim

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
        from app.database.connection import get_session_factory

        poll = get_settings().assessment_poll_interval_seconds
        while not self._stop.is_set():
            claimed = None
            try:
                db = get_session_factory()()
                try:
                    self._reap_stuck(db)
                    claimed = self._claim_if_capacity(db)
                    if claimed is not None:
                        self._execute(db, claimed)
                finally:
                    db.close()
            except Exception as exc:  # never let a worker thread die
                logger.warning("assessment_worker_error error=%s", exc)
            if claimed is None:
                if self._stop.wait(poll):
                    break

    def _claim_if_capacity(self, db):
        """Adaptive throttle (Part 6): only claim a job if the CURRENT OpenAI
        capacity state allows another concurrent assessment. Live interviews keep
        priority, so assessments back off (fewer effective workers) as OpenAI
        approaches its limits, and stop claiming entirely when CRITICAL+paused.
        Workers never die - they just poll without claiming until capacity returns.
        The reservation (in_flight++) happens atomically with the claim so the
        effective-worker cap is respected across all worker threads."""
        from app.core import capacity

        with self._claim_lock:
            effective, _mode = capacity.effective_assessment_workers()
            if get_telemetry().assessment_in_flight.value >= effective:
                return None  # throttled: at (or above) the effective-worker cap
            claimed = self._claim_one(db)
            if claimed is not None:
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
        # NOTE: the in-flight slot is RESERVED at claim time (_claim_if_capacity)
        # so the adaptive cap holds across threads; here we only release it.
        tele = get_telemetry()
        settings = get_settings()
        t0 = time.monotonic()
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
            logger.warning("assessment_job_failed run=%s error=%s", run.id, exc)
        finally:
            tele.assessment_in_flight.dec()
            tele.history  # (touch; no-op) keep import graph obvious
            duration_ms = int((time.monotonic() - t0) * 1000)
            tele.openai.window.observe_latency(duration_ms)  # coarse job duration signal

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
