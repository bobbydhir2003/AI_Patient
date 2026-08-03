"""Transparent capacity analysis for load-test runs (Priority J).

Given ONLY the real measured summary + per-window series produced by the load
worker, this module derives:
  - overallStatus: PASS | PASS_WITH_WARNING | FAIL | INCONCLUSIVE
  - observedBottleneck: what actually degraded first (from measured signals)
  - recommendedSafeCapacity: highest concurrency that stayed healthy in the
    OBSERVED data, with an explicit basis/reason - never a hardcoded number.

Nothing here invents data. If the run was too short or too small to support a
conclusion, the result is INCONCLUSIVE with a reason. Every threshold used is
returned under `criteria` so the judgement is auditable, not a black box.
"""
from __future__ import annotations

# Analysis policy (transparent, surfaced in the response under `criteria`).
MIN_REQUESTS = 20            # too few requests => cannot conclude
MIN_DURATION_S = 20         # too short => cannot conclude
MIN_LOAD_FRACTION = 0.5     # peak concurrency must reach >=50% of target
MIN_HEALTHY_SAMPLES = 3     # need a few in-window samples to see a trend

HEALTHY_SUCCESS_PCT = 99.0  # a window is "healthy" at/above this success rate
FAIL_SUCCESS_PCT = 95.0     # overall success below this => FAIL
WARN_P95_MS = 3000          # p95 above this (but success ok) => warning
FAIL_P95_MS = 15000         # p95 above this => treated as failure-level latency


def _num(x):
    return x if isinstance(x, (int, float)) else None


def analyze(*, target_users: int, duration_seconds: int, ramp_seconds: int,
            overall: dict | None, series: list[dict] | None) -> dict:
    overall = overall or {}
    series = series or []
    criteria = {
        "minRequests": MIN_REQUESTS,
        "minDurationSeconds": MIN_DURATION_S,
        "minLoadFraction": MIN_LOAD_FRACTION,
        "healthySuccessPct": HEALTHY_SUCCESS_PCT,
        "failSuccessPct": FAIL_SUCCESS_PCT,
        "warnP95Ms": WARN_P95_MS,
        "failP95Ms": FAIL_P95_MS,
    }

    total_requests = overall.get("requests") or 0
    success_rate = _num(overall.get("successRate"))
    status_counts = overall.get("statusCounts") or {}
    max_active = overall.get("maxActiveUsers") or 0
    lat = overall.get("latencyMs") or {}
    p95 = _num(lat.get("p95"))
    p99 = _num(lat.get("p99"))

    def _count(prefix_ok):
        return sum(v for k, v in status_counts.items() if prefix_ok(str(k)))

    c5xx = _count(lambda k: k.isdigit() and int(k) >= 500)
    c503 = status_counts.get("503", 0)
    c429 = status_counts.get("429", 0)
    network_errors = overall.get("networkErrors") or 0

    notes: list[str] = []

    # ---------------- INCONCLUSIVE gates ----------------
    incon_reasons = []
    if total_requests < MIN_REQUESTS:
        incon_reasons.append(
            f"Only {total_requests} requests were completed (need ≥{MIN_REQUESTS}).")
    if duration_seconds < MIN_DURATION_S:
        incon_reasons.append(
            f"Run duration {duration_seconds}s is below the {MIN_DURATION_S}s minimum for a conclusion.")
    if target_users > 0 and max_active < target_users * MIN_LOAD_FRACTION:
        incon_reasons.append(
            f"Peak concurrency reached only {max_active} of {target_users} target users "
            f"(<{int(MIN_LOAD_FRACTION * 100)}%).")
    healthy_capable_samples = [s for s in series if _num(s.get("successRate")) is not None]
    if len(healthy_capable_samples) < MIN_HEALTHY_SAMPLES:
        incon_reasons.append(
            f"Only {len(healthy_capable_samples)} measured windows with data (need ≥{MIN_HEALTHY_SAMPLES}).")

    if incon_reasons:
        return {
            "overallStatus": "INCONCLUSIVE",
            "reason": " ".join(incon_reasons),
            "observedBottleneck": {"observed": False,
                                   "detail": "Not enough data to identify a bottleneck."},
            "recommendedSafeCapacity": {"value": None, "basis": "insufficient_data",
                                        "reason": "The run was too short or too small to establish a safe capacity."},
            "summary": _summary(total_requests, success_rate, p95, p99, max_active,
                                 c429, c503, c5xx, network_errors),
            "criteria": criteria,
            "notes": notes,
        }

    # ---------------- Observed bottleneck (from real signals) ----------------
    if c5xx > 0 or c503 > 0:
        bottleneck = {"observed": True, "kind": "server_overload",
                      "detail": f"Server errors under load ({c5xx} × 5xx, {c503} × 503) — "
                                "the application/server saturated before the client did."}
    elif c429 > 0:
        bottleneck = {"observed": True, "kind": "rate_limiting",
                      "detail": f"Rate limiting ({c429} × 429) — an upstream provider or the app's "
                                "own rate limits capped throughput."}
    elif network_errors > 0:
        bottleneck = {"observed": True, "kind": "connection_failures",
                      "detail": f"{network_errors} connection failures — the target refused or dropped "
                                "connections under load."}
    elif p95 is not None and p95 > WARN_P95_MS:
        bottleneck = {"observed": True, "kind": "latency",
                      "detail": f"Response latency grew under load (p95 {p95} ms) without errors — "
                                "processing time is the limiting factor."}
    else:
        bottleneck = {"observed": False,
                      "detail": "No degradation was observed within the tested range; the bottleneck "
                                "is beyond the load that was applied."}

    # ---------------- Safe capacity from the OBSERVED stable region ----------------
    safe = _safe_capacity(series, target_users, max_active)

    # ---------------- Overall status ----------------
    failed = (
        (success_rate is not None and success_rate < FAIL_SUCCESS_PCT)
        or c5xx > 0 or c503 > 0
        or (p95 is not None and p95 > FAIL_P95_MS)
    )
    warned = (
        c429 > 0
        or network_errors > 0
        or (p95 is not None and p95 > WARN_P95_MS)
        or (safe["value"] is not None and target_users > 0 and safe["value"] < target_users)
        or (success_rate is not None and success_rate < HEALTHY_SUCCESS_PCT)
    )
    if failed:
        status = "FAIL"
    elif warned:
        status = "PASS_WITH_WARNING"
    else:
        status = "PASS"

    return {
        "overallStatus": status,
        "reason": _status_reason(status, success_rate, bottleneck, safe, target_users),
        "observedBottleneck": bottleneck,
        "recommendedSafeCapacity": safe,
        "summary": _summary(total_requests, success_rate, p95, p99, max_active,
                            c429, c503, c5xx, network_errors),
        "criteria": criteria,
        "notes": notes,
    }


def _safe_capacity(series: list[dict], target_users: int, max_active: int) -> dict:
    """Highest concurrency level whose measured window stayed healthy."""
    healthy_levels = []
    unhealthy_seen = False
    for s in series:
        active = s.get("activeUsers")
        sr = _num(s.get("successRate"))
        p95 = _num(s.get("p95"))
        if active is None:
            continue
        # A window with no requests (sr is None) is skipped, not counted.
        if sr is None:
            continue
        is_healthy = sr >= HEALTHY_SUCCESS_PCT and (p95 is None or p95 <= WARN_P95_MS)
        if is_healthy:
            healthy_levels.append(active)
        else:
            unhealthy_seen = True

    if not healthy_levels:
        return {"value": None, "basis": "no_healthy_window",
                "reason": "No measured window met the health criteria "
                          f"(≥{HEALTHY_SUCCESS_PCT}% success and p95 ≤ {WARN_P95_MS} ms)."}

    peak_healthy = max(healthy_levels)
    if not unhealthy_seen and target_users and max_active >= target_users:
        return {"value": peak_healthy, "basis": "no_degradation_observed",
                "reason": f"No degradation up to the tested peak of {peak_healthy} concurrent virtual "
                          "students; the true ceiling is at or above this and was not reached."}
    return {"value": peak_healthy, "basis": "observed_stable_region",
            "reason": f"Highest concurrency with ≥{HEALTHY_SUCCESS_PCT}% window success and "
                      f"p95 ≤ {WARN_P95_MS} ms was {peak_healthy} concurrent virtual students; "
                      "degradation appeared above this level."}


def _summary(total, success_rate, p95, p99, max_active, c429, c503, c5xx, net) -> dict:
    return {
        "totalRequests": total,
        "successRatePct": success_rate,
        "p95Ms": p95,
        "p99Ms": p99,
        "peakConcurrency": max_active,
        "rateLimited429": c429,
        "overloaded503": c503,
        "serverErrors5xx": c5xx,
        "networkErrors": net,
    }


def _status_reason(status, success_rate, bottleneck, safe, target_users) -> str:
    if status == "PASS":
        return (f"All requests succeeded ({success_rate}%) with no errors or latency degradation "
                f"across the tested range up to {safe.get('value')} concurrent virtual students.")
    if status == "PASS_WITH_WARNING":
        return (f"The system handled the load ({success_rate}% success) but showed warning signals: "
                f"{bottleneck.get('detail')}")
    if status == "FAIL":
        return f"The system did not sustain the applied load: {bottleneck.get('detail')}"
    return "Inconclusive."
