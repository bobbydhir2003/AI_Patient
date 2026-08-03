"""Process-local rate limiting and login brute-force throttling.

IMPORTANT (deployment): these limiters keep state in memory in a SINGLE process.
With multiple uvicorn/gunicorn workers each worker has its own counters, so the
effective limit is roughly (configured limit x worker count). That is a safe,
conservative abuse/cost guard for the current single-service AWS deployment. If
strict global limits are ever required across workers/instances, back these with
Redis (documented as future work). No third-party rate-limit library is used
because the project does not already depend on one and a small fixed-window
counter is sufficient and fully testable.

Keys prefer the AUTHENTICATED user id when a bearer token is present, so one
student cannot exhaust another's budget; anonymous requests (e.g. login) fall
back to client IP (honoring the first X-Forwarded-For hop behind nginx/ALB).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import jwt
from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.exceptions import LoginThrottledError, RateLimitedError
from app.core.security import decode_access_token

_PERIOD_SECONDS = {
    "second": 1, "sec": 1, "s": 1,
    "minute": 60, "min": 60, "m": 60,
    "hour": 3600, "h": 3600,
}


def parse_rate(rate: str) -> tuple[int, int]:
    """'10/minute' -> (10, 60). Unknown period defaults to per-minute."""
    count, _, period = rate.partition("/")
    return int(count.strip()), _PERIOD_SECONDS.get(period.strip().lower(), 60)


def client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def identity_key(request: Request) -> str:
    """Authenticated user id when available, else client IP."""
    auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        try:
            sub = decode_access_token(token).get("sub")
            if sub:
                return f"user:{sub}"
        except jwt.PyJWTError:
            pass
    return f"ip:{client_ip(request)}"


class _FixedWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        """Register a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            start, count = self._hits.get(key, (now, 0))
            if now - start >= window:
                start, count = now, 0
            count += 1
            self._hits[key] = (start, count)
            if count > limit:
                return False, max(1, int(window - (now - start)))
            return True, 0

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = _FixedWindowLimiter()


def reset_rate_limits() -> None:
    """Test helper: clear all counters."""
    _limiter.clear()


def rate_limit(bucket: str, rate_getter: Callable[[Settings], str]):
    """Build a FastAPI dependency that enforces `rate_getter(settings)` for
    `bucket`, keyed by authenticated identity or IP. No-op when disabled."""

    def dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        limit, window = parse_rate(rate_getter(settings))
        allowed, retry_after = _limiter.hit(f"{bucket}:{identity_key(request)}", limit, window)
        if not allowed:
            raise RateLimitedError(retry_after=retry_after)

    return dependency


# --------------------------------------------------------------------------
#  Login brute-force throttle (A9)
# --------------------------------------------------------------------------
class _LoginThrottle:
    """Per (IP, normalized-email) failed-attempt counter with temporary lockout.

    Never permanently locks an account; a successful login clears the counter.
    """

    def __init__(self) -> None:
        # key -> (window_start, fail_count, locked_until_monotonic)
        self._state: dict[str, tuple[float, int, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(ip: str, email: str) -> str:
        return f"{ip}|{(email or '').strip().lower()}"

    def locked_for(self, ip: str, email: str) -> int:
        """Remaining lockout seconds (0 if not locked)."""
        now = time.monotonic()
        with self._lock:
            _start, _count, locked_until = self._state.get(self._key(ip, email), (now, 0, 0.0))
            if locked_until > now:
                return max(1, int(locked_until - now))
            return 0

    def record_failure(self, ip: str, email: str, settings: Settings) -> None:
        now = time.monotonic()
        window = settings.login_attempt_window_seconds
        with self._lock:
            key = self._key(ip, email)
            start, count, locked_until = self._state.get(key, (now, 0, 0.0))
            if now - start >= window:
                start, count = now, 0
            count += 1
            if count >= settings.login_max_failed_attempts:
                locked_until = now + settings.login_lockout_seconds
                count = 0
                start = now
            self._state[key] = (start, count, locked_until)

    def record_success(self, ip: str, email: str) -> None:
        with self._lock:
            self._state.pop(self._key(ip, email), None)

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


_login_throttle = _LoginThrottle()


def reset_login_throttle() -> None:
    """Test helper: clear all login-throttle state."""
    _login_throttle.clear()


def check_login_allowed(ip: str, email: str) -> None:
    """Raise LoginThrottledError (429, generic) if this IP/email is locked out."""
    settings = get_settings()
    if not settings.login_throttle_enabled:
        return
    remaining = _login_throttle.locked_for(ip, email)
    if remaining > 0:
        raise LoginThrottledError(retry_after=remaining)


def record_login_failure(ip: str, email: str) -> None:
    settings = get_settings()
    if settings.login_throttle_enabled:
        _login_throttle.record_failure(ip, email, settings)


def record_login_success(ip: str, email: str) -> None:
    _login_throttle.record_success(ip, email)
