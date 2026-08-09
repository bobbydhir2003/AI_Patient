"""Server-side aggregation for the AI Usage & Cost dashboard.

All heavy lifting (SUM/GROUP BY over the indexed ai_usage_events table) happens
here so the frontend only ever receives small, already-aggregated payloads. The
dashboard never queries the providers; it reads recorded usage events.

Cost terminology: every cost here is an ESTIMATED usage cost (recorded unit
price × provider-reported usage units), NOT a provider invoice.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.models import AiUsageEvent, InterviewSession, Student

# range key -> (window timedelta, bucket seconds for the timeseries)
_RANGES: dict[str, tuple[timedelta, int]] = {
    "live": (timedelta(minutes=5), 30),
    "5m": (timedelta(minutes=5), 30),
    "15m": (timedelta(minutes=15), 60),
    "1h": (timedelta(hours=1), 300),
    "6h": (timedelta(hours=6), 1800),
    "24h": (timedelta(hours=24), 3600),
    "7d": (timedelta(days=7), 86400),
    "30d": (timedelta(days=30), 86400),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_window(
    range_key: str, start: datetime | None = None, end: datetime | None = None
) -> tuple[datetime, datetime, int]:
    """Return (start, end, bucket_seconds) for a range key or explicit custom range."""
    now = _now()
    if range_key == "today":
        s = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return s, now, 3600
    if range_key == "custom" and start and end:
        span = max(1.0, (end - start).total_seconds())
        # ~120 buckets max, snapped to a sane grid.
        bucket = max(30, int(span / 120))
        return start, end, bucket
    delta, bucket = _RANGES.get(range_key, _RANGES["24h"])
    return now - delta, now, bucket


# --- OpenAI / ElevenLabs conditional sums (portable across sqlite + postgres) -
def _oi(col):
    return func.coalesce(func.sum(case((AiUsageEvent.provider == "openai", col), else_=0)), 0)


def _el(col):
    return func.coalesce(func.sum(case((AiUsageEvent.provider == "elevenlabs", col), else_=0)), 0)


def _cost(provider):
    return func.coalesce(
        func.sum(case((AiUsageEvent.provider == provider, AiUsageEvent.estimated_cost_usd), else_=0.0)),
        0.0,
    )


def _base_totals(db: Session, start: datetime, end: datetime) -> dict:
    row = db.execute(
        select(
            _oi(AiUsageEvent.input_tokens).label("in_tok"),
            _oi(AiUsageEvent.output_tokens).label("out_tok"),
            _el(AiUsageEvent.characters_generated).label("chars"),
            _cost("openai").label("openai_cost"),
            _cost("elevenlabs").label("elevenlabs_cost"),
            func.count(distinct(AiUsageEvent.session_id)).label("sessions"),
        ).where(AiUsageEvent.created_at >= start, AiUsageEvent.created_at < end)
    ).one()
    in_tok = int(row.in_tok or 0)
    out_tok = int(row.out_tok or 0)
    openai_cost = round(float(row.openai_cost or 0.0), 6)
    el_cost = round(float(row.elevenlabs_cost or 0.0), 6)
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "elevenlabs_characters": int(row.chars or 0),
        "openai_cost_usd": openai_cost,
        "elevenlabs_cost_usd": el_cost,
        "total_cost_usd": round(openai_cost + el_cost, 6),
        "session_count": int(row.sessions or 0),
    }


def _projected_monthly(db: Session) -> dict:
    """Projected monthly cost from a rolling daily average of REAL usage.

    Method (documented): avg_daily = SUM(estimated_cost) over the last up-to-7
    days ÷ number of days observed; projected = avg_daily × days_in_current_month.
    Returns available=False with a message when there is not enough usage yet.
    """
    now = _now()
    window_days = 7
    start = now - timedelta(days=window_days)
    total = float(
        db.execute(
            select(func.coalesce(func.sum(AiUsageEvent.estimated_cost_usd), 0.0)).where(
                AiUsageEvent.created_at >= start
            )
        ).scalar_one()
        or 0.0
    )
    # Days actually observed (from the first event in the window, min 1).
    first = db.execute(
        select(func.min(AiUsageEvent.created_at)).where(AiUsageEvent.created_at >= start)
    ).scalar_one()
    if total <= 0 or first is None:
        return {"available": False, "message": "Not enough usage data for projection.", "projected_usd": None}
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    days_observed = max(0.5, (now - first).total_seconds() / 86400.0)
    avg_daily = total / days_observed
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    return {
        "available": True,
        "message": "",
        "projected_usd": round(avg_daily * days_in_month, 2),
        "avg_daily_usd": round(avg_daily, 4),
        "days_in_month": days_in_month,
    }


def summary(db: Session, range_key: str, start=None, end=None) -> dict:
    s, e, _bucket = resolve_window(range_key, start, end)
    totals = _base_totals(db, s, e)

    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_cost_today = float(
        db.execute(
            select(func.coalesce(func.sum(AiUsageEvent.estimated_cost_usd), 0.0)).where(
                AiUsageEvent.created_at >= today_start
            )
        ).scalar_one() or 0.0
    )
    total_cost_mtd = float(
        db.execute(
            select(func.coalesce(func.sum(AiUsageEvent.estimated_cost_usd), 0.0)).where(
                AiUsageEvent.created_at >= month_start
            )
        ).scalar_one() or 0.0
    )

    sessions = totals["session_count"]
    avg_tokens = round(totals["total_tokens"] / sessions) if sessions else 0
    avg_cost = round(totals["total_cost_usd"] / sessions, 4) if sessions else 0.0
    avg_in = round(totals["input_tokens"] / sessions) if sessions else 0
    avg_out = round(totals["output_tokens"] / sessions) if sessions else 0
    avg_chars = round(totals["elevenlabs_characters"] / sessions) if sessions else 0

    total_cost = totals["total_cost_usd"]
    split = {
        "openai_usd": totals["openai_cost_usd"],
        "elevenlabs_usd": totals["elevenlabs_cost_usd"],
        "openai_pct": round(totals["openai_cost_usd"] / total_cost * 100, 1) if total_cost else 0.0,
        "elevenlabs_pct": round(totals["elevenlabs_cost_usd"] / total_cost * 100, 1) if total_cost else 0.0,
    }

    return {
        "range": range_key,
        "start": s.isoformat(),
        "end": e.isoformat(),
        **totals,
        "avg_tokens_per_interview": avg_tokens,
        "avg_input_tokens_per_interview": avg_in,
        "avg_output_tokens_per_interview": avg_out,
        "avg_elevenlabs_chars_per_interview": avg_chars,
        "avg_cost_per_interview_usd": avg_cost,
        "total_cost_today_usd": round(total_cost_today, 6),
        "total_cost_month_to_date_usd": round(total_cost_mtd, 6),
        "provider_split": split,
        "projected_monthly": _projected_monthly(db),
    }


def timeseries(db: Session, range_key: str, start=None, end=None) -> dict:
    """Bucketed input/output/total token series. Buckets are computed in Python
    from the minimal (created_at, tokens) columns for the window — small, indexed
    read; no dialect-specific epoch SQL."""
    s, e, bucket = resolve_window(range_key, start, end)
    rows = db.execute(
        select(
            AiUsageEvent.created_at,
            AiUsageEvent.provider,
            AiUsageEvent.input_tokens,
            AiUsageEvent.output_tokens,
        )
        .where(AiUsageEvent.created_at >= s, AiUsageEvent.created_at < e, AiUsageEvent.provider == "openai")
        .order_by(AiUsageEvent.created_at.asc())
        .limit(50000)
    ).all()

    n_buckets = max(1, min(240, int((e - s).total_seconds() // bucket) + 1))
    buckets = [{"input": 0, "output": 0} for _ in range(n_buckets)]
    s_epoch = s.timestamp()
    for created_at, _provider, in_tok, out_tok in rows:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        idx = int((created_at.timestamp() - s_epoch) // bucket)
        if 0 <= idx < n_buckets:
            buckets[idx]["input"] += int(in_tok or 0)
            buckets[idx]["output"] += int(out_tok or 0)

    points = []
    for i, b in enumerate(buckets):
        ts = datetime.fromtimestamp(s_epoch + i * bucket, tz=timezone.utc)
        points.append({
            "ts": ts.isoformat(),
            "input_tokens": b["input"],
            "output_tokens": b["output"],
            "total_tokens": b["input"] + b["output"],
        })
    return {"range": range_key, "bucket_seconds": bucket, "points": points}


def _session_rows(db: Session, start: datetime, end: datetime, limit: int, session_id: str | None = None):
    q = (
        select(
            AiUsageEvent.session_id.label("session_id"),
            _oi(AiUsageEvent.input_tokens).label("in_tok"),
            _oi(AiUsageEvent.output_tokens).label("out_tok"),
            _el(AiUsageEvent.characters_generated).label("chars"),
            _cost("openai").label("openai_cost"),
            _cost("elevenlabs").label("elevenlabs_cost"),
            func.coalesce(func.sum(case((AiUsageEvent.provider == "openai", AiUsageEvent.request_count), else_=0)), 0).label("openai_requests"),
            func.coalesce(func.sum(case((AiUsageEvent.provider == "elevenlabs", AiUsageEvent.request_count), else_=0)), 0).label("tts_requests"),
            func.max(AiUsageEvent.created_at).label("last_usage"),
        )
        .where(AiUsageEvent.session_id.isnot(None), AiUsageEvent.created_at >= start, AiUsageEvent.created_at < end)
        .group_by(AiUsageEvent.session_id)
        .order_by(func.max(AiUsageEvent.created_at).desc())
    )
    if session_id is not None:
        q = q.where(AiUsageEvent.session_id == session_id)
    else:
        q = q.limit(limit)
    return db.execute(q).all()


def _decorate_session(db: Session, r) -> dict:
    sess = db.get(InterviewSession, r.session_id) if r.session_id else None
    student_name = ""
    if sess is not None:
        student = db.get(Student, sess.student_id)
        student_name = student.name if student else ""
    in_tok = int(r.in_tok or 0)
    out_tok = int(r.out_tok or 0)
    oi_cost = round(float(r.openai_cost or 0.0), 6)
    el_cost = round(float(r.elevenlabs_cost or 0.0), 6)
    last = r.last_usage
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return {
        "session_id": r.session_id,
        "student_name": student_name or "—",
        "case_id": sess.case_id if sess else "",
        "status": sess.status if sess else "unknown",
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "elevenlabs_characters": int(r.chars or 0),
        "openai_requests": int(r.openai_requests or 0),
        "tts_requests": int(r.tts_requests or 0),
        "openai_cost_usd": oi_cost,
        "elevenlabs_cost_usd": el_cost,
        "total_cost_usd": round(oi_cost + el_cost, 6),
        "last_updated": last.isoformat() if last is not None else None,
        "started_at": sess.started_at.isoformat() if sess and sess.started_at else None,
        "completed_at": sess.completed_at.isoformat() if sess and sess.completed_at else None,
    }


def sessions(db: Session, range_key: str, start=None, end=None, limit: int = 25) -> dict:
    s, e, _b = resolve_window(range_key, start, end)
    rows = _session_rows(db, s, e, limit)
    # total distinct sessions with usage in window (for "Displaying X of N")
    total = int(
        db.execute(
            select(func.count(distinct(AiUsageEvent.session_id))).where(
                AiUsageEvent.session_id.isnot(None),
                AiUsageEvent.created_at >= s,
                AiUsageEvent.created_at < e,
            )
        ).scalar_one() or 0
    )
    return {"range": range_key, "total": total, "sessions": [_decorate_session(db, r) for r in rows]}


def session_detail(db: Session, session_id: str) -> dict | None:
    # All-time totals for the specific session (not window-limited).
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows = _session_rows(db, far_past, _now() + timedelta(days=1), 1, session_id=session_id)
    if not rows:
        return None
    detail = _decorate_session(db, rows[0])
    # Duration + cost/minute from the real session timestamps.
    sess = db.get(InterviewSession, session_id)
    duration_min = None
    if sess and sess.started_at:
        endt = sess.completed_at or _now()
        st = sess.started_at
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        if endt.tzinfo is None:
            endt = endt.replace(tzinfo=timezone.utc)
        duration_min = round(max(0.0, (endt - st).total_seconds()) / 60.0, 2)
    detail["duration_minutes"] = duration_min
    detail["cost_per_minute_usd"] = (
        round(detail["total_cost_usd"] / duration_min, 4) if duration_min and duration_min > 0 else None
    )
    return detail
