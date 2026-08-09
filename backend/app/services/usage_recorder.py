"""Records real AI provider usage events (source of truth for the dashboard).

Design goals:
- NEVER slow or break an interview turn. Every function is best-effort: it
  catches all errors and returns without raising.
- Record provider-reported usage ONCE per real request (no per-chunk counting).
- Preserve the unit prices used at record time (historical accuracy).

Callers pass their existing request `db` session (all call sites have one), so
the event is written to the same database the request uses — no extra connection
and correct behavior under test isolation. Recording happens AFTER the turn's own
transaction commits, so committing here never flushes unrelated pending state.
"""
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.core import pricing
from app.core.logging import get_logger

logger = get_logger(__name__)


def _table_ready(db: Session) -> bool:
    try:
        return sa_inspect(db.get_bind()).has_table("ai_usage_events")
    except Exception:
        return False


def record_openai_usage(
    db: Session,
    session_id: str | None,
    student_id: str | None,
    case_id: str | None,
    usage: dict | None,
) -> None:
    """Record ONE completed OpenAI request. `usage` carries provider-reported
    token counts (input_tokens/output_tokens[/cached_input_tokens]/model). A
    missing/empty usage (e.g. an interrupted stream) is skipped — never faked."""
    if not usage:
        return
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0:
        return
    model = str(usage.get("model") or "")
    try:
        if not _table_ready(db):
            return
        from app.models import AiUsageEvent

        cost, rates = pricing.estimate_openai_cost(
            input_tokens=max(0, input_tokens - cached),
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            model=model,
        )
        db.add(
            AiUsageEvent(
                session_id=session_id,
                student_id=student_id,
                case_id=case_id,
                provider="openai",
                model=model,
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                input_unit_price=rates.input_per_token,
                output_unit_price=rates.output_per_token,
                provider_unit_price=0.0,
                pricing_version=pricing.PRICING_VERSION,
                estimated_cost_usd=cost,
                provider_request_id=str(usage.get("request_id")) if usage.get("request_id") else None,
            )
        )
        db.commit()
    except Exception as exc:  # telemetry must never break the interview
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("ai_usage record_openai failed: %s", exc)


def record_elevenlabs_usage(
    db: Session,
    session_id: str | None,
    student_id: str | None,
    case_id: str | None,
    characters: int,
    voice_id: str | None = None,
    model_id: str | None = None,
    audio_seconds: float = 0.0,
    request_id: str | None = None,
) -> None:
    """Record ONE real ElevenLabs TTS synthesis request (characters generated).
    Call only when the provider is actually invoked (cache MISS)."""
    characters = int(characters or 0)
    if characters <= 0:
        return
    try:
        if not _table_ready(db):
            return
        from app.models import AiUsageEvent

        cost, per_char = pricing.estimate_elevenlabs_cost(characters)
        db.add(
            AiUsageEvent(
                session_id=session_id,
                student_id=student_id,
                case_id=case_id,
                provider="elevenlabs",
                model=model_id or "",
                characters_generated=characters,
                audio_seconds=float(audio_seconds or 0.0),
                request_count=1,
                provider_unit_price=per_char,
                pricing_version=pricing.PRICING_VERSION,
                estimated_cost_usd=cost,
                provider_request_id=request_id or (voice_id or None),
            )
        )
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("ai_usage record_elevenlabs failed: %s", exc)
