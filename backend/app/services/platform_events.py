"""AI iEXCEL Course 1: progress reporting to the platform Events API v1.

Only sessions started from a course launch (interview_sessions.context_id set)
are reported; direct-login sessions never are. Each event is written to the
platform_event_outbox in the SAME transaction as the change it describes, with
a deterministic event id (uuid5 of type + session [+ assessment run]), so a
repeated call never creates a second event and the platform's own idempotency
(by eventId) absorbs re-deliveries. Delivery runs in a background thread with
exponential backoff, so a platform outage never blocks or fails an interview.

Minimum necessary data: the platform's opaque subject, the offering id, a case
activity reference, an attempt number, and (for assessments) the run reference
plus the qualitative overall level when verified. Never transcripts, audio,
survey answers, narratives, evidence, names, or numeric scores.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import InterviewSession, PlatformEventOutbox, PlatformIdentity, User

logger = get_logger(__name__)

EVENT_NAMESPACE = uuid.UUID("5d3f7c2e-8a41-4f0b-9c55-1e2a7b6d4c90")  # AI SP -> iEXCEL events
SCHEMA_VERSION = "1.0"
EVENTS_SCOPE = "iexcel.events.write"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
BATCH_SIZE = 50
MAX_ATTEMPTS = 25  # ~ a day of retries at the backoff cap
_BACKOFF_CAP_SECONDS = 3600

STATUS_PENDING = "pending"
STATUS_SENT = "sent"  # applied, stale, or duplicate at the platform
STATUS_REJECTED = "rejected"  # the platform refused it permanently
STATUS_FAILED = "failed"  # gave up after MAX_ATTEMPTS transient failures


def _now() -> datetime:
    return datetime.now(timezone.utc)


def activity_ref(case_id: str) -> str:
    return f"ai-sp/cases/{case_id}"


def _attempt(db: Session, session: InterviewSession) -> int:
    """1-based attempt number of this session among the learner's sessions of
    the same case in the same offering (stable: ordered by start time)."""
    earlier = db.execute(
        select(func.count())
        .select_from(InterviewSession)
        .where(
            InterviewSession.student_id == session.student_id,
            InterviewSession.case_id == session.case_id,
            InterviewSession.context_id == session.context_id,
            InterviewSession.started_at < session.started_at,
        )
    ).scalar_one()
    return int(earlier) + 1


def _subject_for(db: Session, session: InterviewSession) -> str | None:
    issuer = get_settings().lti_platform_issuer
    return db.execute(
        select(PlatformIdentity.subject)
        .join(User, User.id == PlatformIdentity.user_id)
        .where(User.student_id == session.student_id, PlatformIdentity.issuer == issuer)
    ).scalar_one_or_none()


def _enqueue(
    db: Session,
    session: InterviewSession,
    event_type: str,
    key: str,
    data: dict,
    occurred_at: datetime | None,
) -> None:
    subject = _subject_for(db, session)
    if subject is None:
        logger.warning("platform_event_skipped reason=no_identity session_id=%s", session.id)
        return
    event_id = str(uuid.uuid5(EVENT_NAMESPACE, f"{event_type}:{key}"))
    event = {
        "eventId": event_id,
        "type": event_type,
        "schemaVersion": SCHEMA_VERSION,
        "offeringId": session.context_id,
        "subject": subject,
        "activityRef": activity_ref(session.case_id),
        "occurredAt": _aware(occurred_at or _now()).isoformat(),
        "data": data,
    }
    if db.execute(
        select(PlatformEventOutbox.id).where(PlatformEventOutbox.event_id == event_id)
    ).first():
        return  # already recorded: never a second event for the same fact
    try:
        with db.begin_nested():
            db.add(
                PlatformEventOutbox(
                    event_id=event_id,
                    event_type=event_type,
                    context_id=session.context_id,
                    session_id=session.id,
                    payload=json.dumps(event, separators=(",", ":")),
                )
            )
    except IntegrityError:  # a concurrent writer recorded the same event
        pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def record_session_started(db: Session, session: InterviewSession) -> None:
    if session.context_id:
        data = {"attempt": _attempt(db, session)}
        _enqueue(db, session, "activity.started", session.id, data, session.started_at)


def record_session_completed(db: Session, session: InterviewSession) -> None:
    if session.context_id:
        data = {"attempt": _attempt(db, session)}
        _enqueue(db, session, "activity.completed", session.id, data, session.completed_at)


def record_assessment_finished(db: Session, session_id: str) -> None:
    """Called after an assessment run ends. Reports a verified (COMPLETE) or
    flagged (NEEDS_REVIEW) run once; the qualitative level only when verified.
    Never raises: reporting must not affect the assessment."""
    try:
        from app.assessment.assessment_repository import AssessmentRepository

        session = db.get(InterviewSession, session_id)
        if session is None or not session.context_id:
            return
        run = AssessmentRepository(db).latest_for_session(session_id)
        if run is None or run.status not in ("COMPLETE", "NEEDS_REVIEW"):
            return
        data: dict[str, Any] = {
            "assessmentRef": f"ai-sp/assessments/{run.id}",
            "attempt": _attempt(db, session),
        }
        settings = get_settings()
        if settings.lti_report_assessment_level and run.status == "COMPLETE" and run.overall_level:
            data["level"] = run.overall_level
        _enqueue(db, session, "assessment.completed", run.id, data, run.completed_at)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - reporting never breaks assessment
        db.rollback()
        logger.warning(
            "platform_event_enqueue_failed session_id=%s error=%s", session_id, type(exc).__name__
        )


# ---------------------------------------------------------------- delivery


class PlatformClient:
    """OAuth client credentials (private_key_jwt) + Events API calls."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http
        self._token: str | None = None
        self._token_expires = 0.0
        self._key = None
        self._key_path = ""

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=10.0)
        return self._http

    def _signing_key(self):
        settings = get_settings()
        if self._key is None or self._key_path != settings.lti_tool_key_file:
            self._key = Path(settings.lti_tool_key_file).read_bytes()
            self._key_path = settings.lti_tool_key_file
        return self._key

    def _assertion(self) -> str:
        settings = get_settings()
        now = int(time.time())
        claims = {
            "iss": settings.lti_client_id,
            "sub": settings.lti_client_id,
            "aud": settings.lti_token_url,
            "iat": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(
            claims, self._signing_key(), algorithm="RS256", headers={"kid": settings.lti_tool_key_id}
        )

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires - 30:
            return self._token
        settings = get_settings()
        response = self._client().post(
            settings.lti_token_url,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": self._assertion(),
                "scope": EVENTS_SCOPE,
            },
        )
        if response.status_code != 200:
            raise _TransientError(f"token_http_{response.status_code}")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + float(body.get("expires_in", 300))
        return self._token

    def send(self, events: list[dict]) -> list[dict]:
        settings = get_settings()
        response = self._client().post(
            settings.lti_events_url,
            json={"events": events},
            headers={"Authorization": f"Bearer {self.access_token()}"},
        )
        if response.status_code == 401:
            self._token = None
            raise _TransientError("events_http_401")
        if response.status_code != 200:
            raise _TransientError(f"events_http_{response.status_code}")
        return response.json()["results"]


class _TransientError(Exception):
    pass


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(10 * 2 ** max(attempts - 1, 0), _BACKOFF_CAP_SECONDS))


def deliver_pending(db: Session, client: PlatformClient, *, limit: int = BATCH_SIZE) -> dict[str, int]:
    """Send one batch of due events. Returns counts by outcome."""
    now = _now()
    stmt = (
        select(PlatformEventOutbox)
        .where(
            PlatformEventOutbox.status == STATUS_PENDING,
            PlatformEventOutbox.next_attempt_at <= now,
        )
        .order_by(PlatformEventOutbox.created_at)
        .limit(limit)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = list(db.execute(stmt).scalars())
    counts = {"sent": 0, "rejected": 0, "retry": 0}
    if not rows:
        db.commit()
        return counts
    try:
        results = client.send([json.loads(r.payload) for r in rows])
    except (_TransientError, httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        code = str(exc) if isinstance(exc, _TransientError) else type(exc).__name__
        for row in rows:
            row.attempts += 1
            row.last_error = code[:120]
            row.next_attempt_at = now + _backoff(row.attempts)
            if row.attempts >= MAX_ATTEMPTS:
                row.status = STATUS_FAILED
            counts["retry"] += 1
        db.commit()
        logger.warning("platform_events_delivery_deferred count=%d error=%s", len(rows), code)
        return counts

    by_id = {r.get("eventId"): r for r in results}
    for row in rows:
        row.attempts += 1
        result = by_id.get(row.event_id)
        if result is None:
            row.last_error = "missing_result"
            row.next_attempt_at = now + _backoff(row.attempts)
            counts["retry"] += 1
            continue
        status = result.get("status")
        if status in ("applied", "stale", "duplicate"):
            row.status, row.sent_at, row.last_error = STATUS_SENT, now, None
            counts["sent"] += 1
        else:  # rejected / conflict: permanent for this payload
            row.status = STATUS_REJECTED
            row.last_error = str(result.get("errorCode") or status)[:120]
            counts["rejected"] += 1
            logger.warning(
                "platform_event_rejected event_id=%s type=%s error=%s",
                row.event_id, row.event_type, row.last_error,
            )
    db.commit()
    return counts


class PlatformEventWorker:
    """One background thread per process. Safe with several processes: rows are
    claimed with FOR UPDATE SKIP LOCKED on PostgreSQL, and the platform
    de-duplicates by eventId in any case."""

    def __init__(self, interval_seconds: float = 5.0) -> None:
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = PlatformClient()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="platform-events", daemon=True)
        self._thread.start()
        logger.info("platform_event_worker_started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        from app.database.connection import get_session_factory

        while not self._stop.is_set():
            try:
                db = get_session_factory()()
                try:
                    counts = deliver_pending(db, self._client)
                finally:
                    db.close()
                if counts["sent"] or counts["rejected"]:
                    logger.info(
                        "platform_events_delivered sent=%d rejected=%d",
                        counts["sent"], counts["rejected"],
                    )
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.warning("platform_event_worker_error error=%s", type(exc).__name__)
            self._stop.wait(self.interval)


_worker: PlatformEventWorker | None = None


def get_platform_event_worker() -> PlatformEventWorker:
    global _worker
    if _worker is None:
        _worker = PlatformEventWorker()
    return _worker
