"""Real OpenAI capacity state, derived from measured provider usage.

Token usage is recorded in ONE place (app/patient_engine/openai_client.py, using
the provider-reported `response.usage` for non-streaming and the streaming
`response.completed` usage event) and read here. This module never estimates
tokens - if the provider did not report usage, it simply isn't counted.

Capacity state is driven by the TIGHTER of TPM and RPM utilization against the
configured limits, so we protect against whichever ceiling we approach first.
Everything is process-local (per worker); see docs/TRAFFIC.md.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.telemetry import get_telemetry

# Effective assessment-worker allowance shrinks as capacity tightens.
THROTTLE_NORMAL = "NORMAL"
THROTTLE_REDUCED = "REDUCED"
THROTTLE_MINIMAL = "MINIMAL"
THROTTLE_PAUSED = "PAUSED"

STATE_NORMAL = "NORMAL"
STATE_BUSY = "BUSY"
STATE_PROTECTING = "PROTECTING"
STATE_CRITICAL = "CRITICAL"


def openai_capacity() -> dict:
    """Snapshot of real OpenAI capacity for the last 60 seconds."""
    s = get_settings()
    win = get_telemetry().openai.window
    tpm_used = win.sum("total_tokens", 60)
    rpm_used = win.sum("requests", 60)
    tpm_limit = max(1, s.openai_tpm_limit)
    rpm_limit = max(1, s.openai_rpm_limit)
    tpm_pct = round(tpm_used / tpm_limit, 4)
    rpm_pct = round(rpm_used / rpm_limit, 4)
    utilization = max(tpm_pct, rpm_pct)

    if utilization >= s.openai_capacity_critical_pct:
        state = STATE_CRITICAL
    elif utilization >= s.openai_capacity_protecting_pct:
        state = STATE_PROTECTING
    elif utilization >= s.openai_capacity_busy_pct:
        state = STATE_BUSY
    else:
        state = STATE_NORMAL

    return {
        "capacity_state": state,
        "utilization_pct": round(utilization, 4),
        "tpm_used": tpm_used,
        "tpm_limit": tpm_limit,
        "tpm_pct": tpm_pct,
        "rpm_used": rpm_used,
        "rpm_limit": rpm_limit,
        "rpm_pct": rpm_pct,
        "headroom_tokens": max(0, tpm_limit - tpm_used),
        "tokens_5m": win.sum("total_tokens", 300),
    }


def capacity_state() -> str:
    return openai_capacity()["capacity_state"]


def effective_assessment_workers(state: str | None = None) -> tuple[int, str]:
    """Return (effective_worker_count, throttle_mode) for the given capacity
    state. Live interviews always keep priority, so assessments back off first."""
    s = get_settings()
    state = state or capacity_state()
    configured = max(1, s.assessment_worker_concurrency)
    if state == STATE_CRITICAL:
        if s.assessment_pause_on_critical:
            return 0, THROTTLE_PAUSED
        return max(1, s.assessment_workers_protecting), THROTTLE_MINIMAL
    if state == STATE_PROTECTING:
        return max(0, min(configured, s.assessment_workers_protecting)), THROTTLE_MINIMAL
    if state == STATE_BUSY:
        return max(0, min(configured, s.assessment_workers_busy)), THROTTLE_REDUCED
    return configured, THROTTLE_NORMAL
