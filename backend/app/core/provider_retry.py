"""Transient-failure retry/backoff for paid providers, plus telemetry recording.

Retries ONLY transient conditions (HTTP 429/408/5xx, timeouts, connection
errors) with exponential backoff + jitter, honoring Retry-After when present.
NEVER retries auth (401/403), bad requests (400/404/422) or validation errors -
retrying those wastes money and cannot succeed. Retry counts, 429s, latency and
active gauges are recorded into the provider's telemetry.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.telemetry import ProviderMetrics

logger = get_logger(__name__)

# Exception class names that must NEVER be retried (from the OpenAI SDK / us).
_NON_RETRYABLE_NAMES = {
    "AuthenticationError", "PermissionDeniedError", "BadRequestError",
    "NotFoundError", "UnprocessableEntityError", "ValidationFailedError",
    "StructuredOutputTruncatedError",
}
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}
# Retryable transient status codes.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class RetryDecision:
    retry: bool
    rate_limited: bool
    retry_after: float | None


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


def _retry_after_of(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        for key in ("retry-after", "Retry-After"):
            val = headers.get(key) if hasattr(headers, "get") else None
            if val:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
    return None


def classify(exc: Exception) -> RetryDecision:
    name = type(exc).__name__
    status = _status_of(exc)
    retry_after = _retry_after_of(exc)
    is_429 = status == 429 or name == "RateLimitError"

    if name in _NON_RETRYABLE_NAMES or (status in _NON_RETRYABLE_STATUS):
        return RetryDecision(retry=False, rate_limited=is_429, retry_after=retry_after)
    if is_429:
        return RetryDecision(retry=True, rate_limited=True, retry_after=retry_after)
    if status in _RETRYABLE_STATUS or name in ("APITimeoutError", "APIConnectionError", "InternalServerError"):
        return RetryDecision(retry=True, rate_limited=False, retry_after=retry_after)
    # Timeouts / transport errors (httpx) - retry.
    try:
        import httpx
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return RetryDecision(retry=True, rate_limited=False, retry_after=retry_after)
    except Exception:
        pass
    # Unknown: do NOT retry (avoid double-charging on ambiguous failures).
    return RetryDecision(retry=False, rate_limited=is_429, retry_after=retry_after)


def backoff_seconds(attempt: int, base_ms: int, max_ms: int, retry_after: float | None) -> float:
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, max_ms / 1000.0)
    exp = base_ms * (2 ** attempt)
    jitter = random.uniform(0, base_ms)
    return min(exp + jitter, max_ms) / 1000.0


def call_with_retry(fn, *, provider: ProviderMetrics, max_retries: int,
                    base_ms: int, max_ms: int, sleep=time.sleep):
    """Run fn() with retry/backoff and record telemetry. Re-raises the last
    exception if all attempts fail or the error is non-retryable."""
    provider.active.inc()
    t0 = time.monotonic()
    retries = 0
    rate_limited = False
    try:
        attempt = 0
        while True:
            try:
                result = fn()
                provider.record(latency_ms=(time.monotonic() - t0) * 1000, ok=True,
                                rate_limited=False, retries=retries)
                return result
            except Exception as exc:
                decision = classify(exc)
                if decision.rate_limited:
                    rate_limited = True
                    provider.window.incr("rate_limited")
                if decision.retry and attempt < max_retries:
                    delay = backoff_seconds(attempt, base_ms, max_ms, decision.retry_after)
                    logger.warning(
                        "provider_retry provider=%s attempt=%d/%d rate_limited=%s delay_s=%.2f err=%s",
                        provider.name, attempt + 1, max_retries, decision.rate_limited, delay, type(exc).__name__,
                    )
                    attempt += 1
                    retries += 1
                    sleep(delay)
                    continue
                provider.record(latency_ms=(time.monotonic() - t0) * 1000, ok=False,
                                rate_limited=False, retries=retries)
                raise
    finally:
        provider.active.dec()
