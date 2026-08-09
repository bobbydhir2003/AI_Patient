"""Interview waiting queue — real, capacity-gated admission for live interviews.

There was no whole-interview queue before this: the existing `interview_slot`
semaphore only bounds the per-turn OpenAI call. This adds a real FIFO waiting
queue that gates admission to a NEW interview when the system is at capacity, so
a busy student sees a queue position instead of a raw 503.

Capacity is REAL, never faked:
    capacity_used = (active interview sessions in the DB, within a recent window)
                    + (transient admission reservations held during the brief
                       gap between "you're admitted" and "session created")
    limit         = settings.max_concurrent_ai_interviews
A slot is free when capacity_used < limit.

Cross-worker: backed by Redis (sorted set for FIFO order + per-entry hashes) when
configured, matching the DistributedSemaphore pattern; falls back to an
in-process store for single-worker dev/test. The DB-derived active count is
shared by all workers regardless of backend.

IMPORTANT separation of concerns: leaving the queue ONLY removes the waiting
entry (and any transient reservation). It NEVER touches interview sessions,
transcripts, assessments, or grading — those are entirely separate lifecycles.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.constants import SESSION_STATUS_ACTIVE, SESSION_STATUS_COMPLETED
from app.core.logging import get_logger

logger = get_logger(__name__)

# Tunables (seconds). Kept local — small, operational, not user-facing config.
RESERVATION_TTL = 120       # admit -> session-create grace window
ENTRY_TTL = 30              # a waiter not polled within this is considered gone
_SLOT_WINDOW = timedelta(hours=1)  # ACTIVE sessions older than this are stale


def _now() -> float:
    return time.time()


def _limit() -> int:
    return max(1, get_settings().max_concurrent_ai_interviews)


# ---------------------------------------------------------------------------
#  Real DB-derived active interview count (shared across workers)
# ---------------------------------------------------------------------------
def active_interview_count(db) -> int:
    """Count interviews genuinely in progress: ACTIVE sessions started within the
    recent window (excludes long-abandoned ACTIVE rows so they can't block the
    queue forever). Real state — never estimated."""
    from sqlalchemy import func, select

    from app.models import InterviewSession

    cutoff = datetime.now(timezone.utc) - _SLOT_WINDOW
    return int(
        db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.status == SESSION_STATUS_ACTIVE,
                InterviewSession.started_at >= cutoff,
            )
        ).scalar_one()
        or 0
    )


def _avg_interview_minutes(db) -> float | None:
    """Average duration of recently completed interviews, for a real wait
    estimate. None when there is no completed interview to learn from."""
    from sqlalchemy import select

    from app.models import InterviewSession

    rows = db.execute(
        select(InterviewSession.started_at, InterviewSession.completed_at)
        .where(
            InterviewSession.status == SESSION_STATUS_COMPLETED,
            InterviewSession.completed_at.isnot(None),
        )
        .order_by(InterviewSession.completed_at.desc())
        .limit(20)
    ).all()
    durations = []
    for started, completed in rows:
        if not started or not completed:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        secs = (completed - started).total_seconds()
        if 5 <= secs <= 3600:
            durations.append(secs / 60.0)
    if not durations:
        return None
    return sum(durations) / len(durations)


# ---------------------------------------------------------------------------
#  In-process store (dev/test/single worker)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_entries: dict[str, dict] = {}          # entry_id -> {student_id, case_id, joined_at, last_seen}
_student_index: dict[str, str] = {}     # student_id -> entry_id
_reservations: dict[str, float] = {}    # student_id -> expiry_epoch


def _prune_local() -> None:
    now = _now()
    for sid, exp in list(_reservations.items()):
        if exp <= now:
            _reservations.pop(sid, None)
    for eid, e in list(_entries.items()):
        if now - e["last_seen"] > ENTRY_TTL:
            _entries.pop(eid, None)
            if _student_index.get(e["student_id"]) == eid:
                _student_index.pop(e["student_id"], None)


def _ordered_local() -> list[str]:
    return [e[0] for e in sorted(_entries.items(), key=lambda kv: kv[1]["joined_at"])]


def _reservation_count_local() -> int:
    now = _now()
    return sum(1 for exp in _reservations.values() if exp > now)


# ---------------------------------------------------------------------------
#  Redis helpers (production; mirrors the in-process semantics)
# ---------------------------------------------------------------------------
_Q_KEY = "ptai:iq:queue"          # ZSET member=entry_id score=joined_at
_RES_KEY = "ptai:iq:reserved"     # ZSET member=student_id score=expiry
_STU_KEY = "ptai:iq:student"      # HASH student_id -> entry_id
_ENTRY_KEY = "ptai:iq:entry:"     # HASH per entry


def _use_redis() -> bool:
    from app.core.redis_client import ping, redis_configured

    return redis_configured() and ping()


def _client():
    from app.core.redis_client import get_redis_client

    return get_redis_client()


def _prune_redis(c) -> None:
    now = _now()
    c.zremrangebyscore(_RES_KEY, "-inf", now)
    # Expire stale entries by last_seen (stored in the entry hash).
    for eid in c.zrange(_Q_KEY, 0, -1):
        eid = eid.decode() if isinstance(eid, bytes) else eid
        h = c.hgetall(_ENTRY_KEY + eid)
        if not h:
            c.zrem(_Q_KEY, eid)
            continue
        last_seen = float(h.get(b"last_seen") or h.get("last_seen") or 0)
        if now - last_seen > ENTRY_TTL:
            student = (h.get(b"student_id") or h.get("student_id") or b"")
            student = student.decode() if isinstance(student, bytes) else student
            c.zrem(_Q_KEY, eid)
            c.delete(_ENTRY_KEY + eid)
            if c.hget(_STU_KEY, student) in (eid, eid.encode()):
                c.hdel(_STU_KEY, student)


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------
def _reserve(student_id: str) -> None:
    exp = _now() + RESERVATION_TTL
    if _use_redis():
        _client().zadd(_RES_KEY, {student_id: exp})
    else:
        _reservations[student_id] = exp


def release_reservation(student_id: str) -> None:
    """Release a transient admission hold (e.g. student left, or the session is
    now ACTIVE and counted by the DB). Best-effort; also expires via TTL."""
    try:
        if _use_redis():
            _client().zrem(_RES_KEY, student_id)
        else:
            _reservations.pop(student_id, None)
    except Exception:
        pass


def _capacity_used(db) -> int:
    active = active_interview_count(db)
    reserved = (
        int(_client().zcount(_RES_KEY, _now(), "+inf")) if _use_redis() else _reservation_count_local()
    )
    return active + reserved


def _estimated_wait_minutes(db, position: int) -> int | None:
    avg = _avg_interview_minutes(db)
    if avg is None or position <= 0:
        return None
    import math

    # Rounds of `limit` interviews must finish ahead of you.
    rounds = math.ceil(position / _limit())
    return max(1, int(round(rounds * avg)))


def join(db, student_id: str, case_id: str) -> dict:
    """Join the queue (idempotent per student). Returns admission or a position.

    - If a slot is free and no one is ahead → admitted immediately (a transient
      reservation is held for RESERVATION_TTL to cover the create gap).
    - Otherwise the student is enqueued and gets a real position.
    A student already in the queue gets their SAME entry back (refresh / double
    click safe — no duplicate entries)."""
    if _use_redis():
        return _join_redis(db, student_id, case_id)
    with _lock:
        _prune_local()
        existing = _student_index.get(student_id)
        if existing and existing in _entries:
            _entries[existing]["last_seen"] = _now()
            pos = _ordered_local().index(existing) + 1
            return _status_payload(db, existing, pos, admitted=False)

        order = _ordered_local()
        if _capacity_used(db) < _limit() and not order:
            _reserve(student_id)
            return {"admitted": True, "entry_id": None, "position": 0, "state": "admitted"}

        entry_id = uuid.uuid4().hex
        now = _now()
        _entries[entry_id] = {"student_id": student_id, "case_id": case_id, "joined_at": now, "last_seen": now}
        _student_index[student_id] = entry_id
        pos = _ordered_local().index(entry_id) + 1
        return _status_payload(db, entry_id, pos, admitted=False)


def status(db, entry_id: str) -> dict:
    """Poll an entry. Admits it when it reaches the front AND a slot is free.
    Heartbeats the entry so an open tab is not pruned."""
    if _use_redis():
        return _status_redis(db, entry_id)
    with _lock:
        _prune_local()
        e = _entries.get(entry_id)
        if not e:
            return {"admitted": False, "state": "expired", "position": None, "entry_id": entry_id}
        e["last_seen"] = _now()
        order = _ordered_local()
        idx = order.index(entry_id)
        if idx == 0 and _capacity_used(db) < _limit():
            student_id = e["student_id"]
            _entries.pop(entry_id, None)
            _student_index.pop(student_id, None)
            _reserve(student_id)
            return {"admitted": True, "entry_id": entry_id, "position": 0, "state": "admitted"}
        return _status_payload(db, entry_id, idx + 1, admitted=False)


def leave(db, entry_id: str, student_id: str | None = None) -> dict:
    """Remove ONLY the waiting entry + its transient reservation. Never touches
    interview sessions, transcripts, assessments or grading."""
    if _use_redis():
        return _leave_redis(entry_id, student_id)
    with _lock:
        e = _entries.pop(entry_id, None)
        sid = student_id or (e["student_id"] if e else None)
        if e and _student_index.get(e["student_id"]) == entry_id:
            _student_index.pop(e["student_id"], None)
        if sid:
            _reservations.pop(sid, None)
    logger.info("queue_leave entry_id=%s (session/assessment untouched)", entry_id)
    return {"left": True}


def _status_payload(db, entry_id: str, position: int, *, admitted: bool) -> dict:
    total = _queue_length(db)
    return {
        "admitted": admitted,
        "entry_id": entry_id,
        "position": position,
        "ahead": max(0, position - 1),
        "total_waiting": total,
        "limit": _limit(),
        "state": "waiting",
        "estimated_wait_minutes": _estimated_wait_minutes(db, position),
    }


def _queue_length(db) -> int:
    if _use_redis():
        try:
            return int(_client().zcard(_Q_KEY))
        except Exception:
            return 0
    return len(_entries)


# ---------------------------------------------------------------------------
#  Redis implementations (mirror the in-process semantics)
# ---------------------------------------------------------------------------
def _join_redis(db, student_id: str, case_id: str) -> dict:
    c = _client()
    _prune_redis(c)
    existing = c.hget(_STU_KEY, student_id)
    existing = existing.decode() if isinstance(existing, bytes) else existing
    if existing and c.exists(_ENTRY_KEY + existing):
        c.hset(_ENTRY_KEY + existing, "last_seen", _now())
        pos = int(c.zrank(_Q_KEY, existing) or 0) + 1
        return _status_payload(db, existing, pos, admitted=False)
    if _capacity_used(db) < _limit() and int(c.zcard(_Q_KEY)) == 0:
        _reserve(student_id)
        return {"admitted": True, "entry_id": None, "position": 0, "state": "admitted"}
    entry_id = uuid.uuid4().hex
    now = _now()
    c.zadd(_Q_KEY, {entry_id: now})
    c.hset(_ENTRY_KEY + entry_id, mapping={"student_id": student_id, "case_id": case_id, "joined_at": now, "last_seen": now})
    c.hset(_STU_KEY, student_id, entry_id)
    pos = int(c.zrank(_Q_KEY, entry_id) or 0) + 1
    return _status_payload(db, entry_id, pos, admitted=False)


def _status_redis(db, entry_id: str) -> dict:
    c = _client()
    _prune_redis(c)
    if not c.exists(_ENTRY_KEY + entry_id):
        return {"admitted": False, "state": "expired", "position": None, "entry_id": entry_id}
    c.hset(_ENTRY_KEY + entry_id, "last_seen", _now())
    rank = c.zrank(_Q_KEY, entry_id)
    if rank is None:
        return {"admitted": False, "state": "expired", "position": None, "entry_id": entry_id}
    if int(rank) == 0 and _capacity_used(db) < _limit():
        h = c.hgetall(_ENTRY_KEY + entry_id)
        student = h.get(b"student_id") or h.get("student_id") or b""
        student = student.decode() if isinstance(student, bytes) else student
        c.zrem(_Q_KEY, entry_id)
        c.delete(_ENTRY_KEY + entry_id)
        c.hdel(_STU_KEY, student)
        _reserve(student)
        return {"admitted": True, "entry_id": entry_id, "position": 0, "state": "admitted"}
    return _status_payload(db, entry_id, int(rank) + 1, admitted=False)


def _leave_redis(entry_id: str, student_id: str | None) -> dict:
    c = _client()
    h = c.hgetall(_ENTRY_KEY + entry_id)
    sid = student_id
    if not sid and h:
        s = h.get(b"student_id") or h.get("student_id") or b""
        sid = s.decode() if isinstance(s, bytes) else s
    c.zrem(_Q_KEY, entry_id)
    c.delete(_ENTRY_KEY + entry_id)
    if sid:
        if c.hget(_STU_KEY, sid) in (entry_id, entry_id.encode()):
            c.hdel(_STU_KEY, sid)
        c.zrem(_RES_KEY, sid)
    logger.info("queue_leave entry_id=%s (session/assessment untouched)", entry_id)
    return {"left": True}


def _reset_for_tests() -> None:
    with _lock:
        _entries.clear()
        _student_index.clear()
        _reservations.clear()
