"""Process-local traffic telemetry.

Everything here is IN-MEMORY and PER-PROCESS (one registry per uvicorn worker).
It never writes a DB row per request. All structures are bounded:
- rolling counters use fixed-width time buckets and discard old buckets;
- latency samples are capped per bucket;
- the active-user and live-session registries are pruned by inactivity;
- the history sampler and alert log are bounded deques.

IMPORTANT (honesty): these numbers describe THIS process only. With multiple
workers, HTTP/provider/active-user numbers are per-worker; see docs/TRAFFIC.md.
No value here is fabricated - a metric that cannot be measured is simply absent.
"""
from __future__ import annotations

import math
import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
#  Rolling, bucketed counters + latency samples (bounded)
# ---------------------------------------------------------------------------
class RollingWindow:
    def __init__(self, bucket_seconds: int = 10, window_seconds: int = 3600, max_samples: int = 250):
        self.bucket_seconds = bucket_seconds
        self.window_seconds = window_seconds
        self.max_buckets = window_seconds // bucket_seconds + 2
        self.max_samples = max_samples
        self._buckets: dict[int, dict] = {}
        self._lock = threading.Lock()

    def _idx(self, now: float) -> int:
        return int(now // self.bucket_seconds)

    def _prune(self, now: float) -> None:
        cutoff = self._idx(now) - self.max_buckets
        for k in [k for k in self._buckets if k < cutoff]:
            del self._buckets[k]

    def _bucket(self, now: float) -> dict:
        idx = self._idx(now)
        b = self._buckets.get(idx)
        if b is None:
            self._prune(now)
            b = {"c": defaultdict(int), "lat": []}
            self._buckets[idx] = b
        return b

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._bucket(time.monotonic())["c"][name] += n

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            lat = self._bucket(time.monotonic())["lat"]
            if len(lat) < self.max_samples:
                lat.append(ms)
            else:  # reservoir replacement keeps the sample unbiased and bounded
                lat[random.randint(0, self.max_samples - 1)] = ms

    def _idxs_in(self, seconds: int, now: float) -> range:
        hi = self._idx(now)
        lo = hi - math.ceil(seconds / self.bucket_seconds)
        return range(lo, hi + 1)

    def sum(self, name: str, seconds: int) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(self._buckets[i]["c"].get(name, 0) for i in self._idxs_in(seconds, now) if i in self._buckets)

    def rate_per_min(self, name: str, seconds: int) -> float:
        total = self.sum(name, seconds)
        return round(total / (seconds / 60.0), 1) if seconds else 0.0

    def latencies(self, seconds: int) -> list[float]:
        now = time.monotonic()
        with self._lock:
            out: list[float] = []
            for i in self._idxs_in(seconds, now):
                b = self._buckets.get(i)
                if b:
                    out.extend(b["lat"])
            return out

    def percentile(self, seconds: int, p: float) -> int | None:
        lat = self.latencies(seconds)
        if not lat:
            return None
        lat.sort()
        k = min(len(lat) - 1, int(round((p / 100.0) * (len(lat) - 1))))
        return int(round(lat[k]))

    def avg_latency(self, seconds: int) -> int | None:
        lat = self.latencies(seconds)
        return int(round(sum(lat) / len(lat))) if lat else None

    def series(self, name: str, seconds: int, step_seconds: int) -> list[dict]:
        """Bucketed time series for charts: [{t, v}] where v is the count in each
        step-sized window over the last `seconds`."""
        now = time.monotonic()
        wall = time.time()
        steps = max(1, seconds // step_seconds)
        out = []
        with self._lock:
            for s in range(steps, 0, -1):
                win_hi = now - (s - 1) * step_seconds
                win_lo = win_hi - step_seconds
                total = 0
                for i in self._buckets:
                    bt = i * self.bucket_seconds
                    if win_lo <= bt < win_hi:
                        total += self._buckets[i]["c"].get(name, 0)
                ts = wall - (s - 1) * step_seconds
                out.append({"t": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "v": total})
        return out


# ---------------------------------------------------------------------------
#  In-flight gauges
# ---------------------------------------------------------------------------
class Gauge:
    def __init__(self) -> None:
        self._v = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self._v += 1

    def dec(self) -> None:
        with self._lock:
            self._v = max(0, self._v - 1)

    @property
    def value(self) -> int:
        with self._lock:
            return self._v


class _GaugeScope:
    def __init__(self, gauge: Gauge, on_exit=None):
        self._g = gauge
        self._on_exit = on_exit

    def __enter__(self):
        self._g.inc()
        return self

    def __exit__(self, *exc):
        self._g.dec()
        if self._on_exit:
            self._on_exit(*exc)
        return False


# ---------------------------------------------------------------------------
#  Provider metrics (OpenAI / ElevenLabs)
# ---------------------------------------------------------------------------
class ProviderMetrics:
    def __init__(self, name: str):
        self.name = name
        self.window = RollingWindow()
        self.active = Gauge()

    def record(self, *, latency_ms: float | None, ok: bool, rate_limited: bool = False, retries: int = 0) -> None:
        self.window.incr("requests")
        self.window.incr("success" if ok else "failure")
        if rate_limited:
            self.window.incr("rate_limited")
        if retries:
            self.window.incr("retries", retries)
        if latency_ms is not None:
            self.window.observe_latency(latency_ms)

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """SINGLE authoritative token-usage recording path (called from the
        OpenAI client with provider-reported usage). Never call this from the
        retry helper - that would double-count."""
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        self.window.incr("input_tokens", input_tokens)
        self.window.incr("output_tokens", output_tokens)
        self.window.incr("total_tokens", input_tokens + output_tokens)

    def tokens_last(self, seconds: int) -> dict:
        return {
            "input": self.window.sum("input_tokens", seconds),
            "output": self.window.sum("output_tokens", seconds),
            "total": self.window.sum("total_tokens", seconds),
        }

    def snapshot(self) -> dict:
        return {
            "active": self.active.value,
            "requests_per_minute": self.window.rate_per_min("requests", 60),
            "requests_last_5m": self.window.sum("requests", 300),
            "success_last_5m": self.window.sum("success", 300),
            "failure_last_5m": self.window.sum("failure", 300),
            "errors_last_5m": self.window.sum("failure", 300),
            "rate_limits_last_5m": self.window.sum("rate_limited", 300),
            "retries_last_5m": self.window.sum("retries", 300),
            "avg_ms": self.window.avg_latency(300),
            "p95_ms": self.window.percentile(300, 95),
        }

    def success_rate_5m(self) -> float | None:
        total = self.window.sum("requests", 300)
        if not total:
            return None
        return round(self.window.sum("success", 300) / total, 4)


# ---------------------------------------------------------------------------
#  Active-user + live-session registries (pruned by inactivity)
# ---------------------------------------------------------------------------
@dataclass
class LiveSession:
    session_id: str
    student_name: str
    student_number: str
    case_id: str
    case_name: str
    status: str = "INTERVIEWING"
    started_at: float = field(default_factory=time.time)          # wall clock
    last_activity_m: float = field(default_factory=time.monotonic)  # monotonic
    last_activity_w: float = field(default_factory=time.time)
    latest_latency_ms: int | None = None


class LiveRegistry:
    def __init__(self) -> None:
        self._users: dict[str, float] = {}          # user_id -> last_seen monotonic
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    # --- users ---
    def touch_user(self, user_id: str) -> None:
        if not user_id:
            return
        with self._lock:
            self._users[user_id] = time.monotonic()

    def active_users(self, window_seconds: int) -> int:
        now = time.monotonic()
        with self._lock:
            for uid in [u for u, t in self._users.items() if now - t > window_seconds]:
                del self._users[uid]
            return len(self._users)

    # --- sessions ---
    def start_session(self, session_id, student_name, student_number, case_id, case_name) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                self._sessions[session_id] = LiveSession(
                    session_id=session_id, student_name=student_name, student_number=student_number,
                    case_id=case_id, case_name=case_name,
                )
            else:
                s.last_activity_m = time.monotonic()
                s.last_activity_w = time.time()

    def set_status(self, session_id, status, latency_ms: int | None = None) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return
            s.status = status
            s.last_activity_m = time.monotonic()
            s.last_activity_w = time.time()
            if latency_ms is not None:
                s.latest_latency_ms = latency_ms

    def complete_session(self, session_id) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.status = "COMPLETED"
                s.last_activity_m = time.monotonic()
                s.last_activity_w = time.time()

    def list_sessions(self, idle_prune_seconds: int) -> list[LiveSession]:
        now = time.monotonic()
        with self._lock:
            for sid in [k for k, s in self._sessions.items() if now - s.last_activity_m > idle_prune_seconds]:
                del self._sessions[sid]
            return sorted(self._sessions.values(), key=lambda s: s.last_activity_m, reverse=True)

    def status_counts(self, idle_prune_seconds: int) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for s in self.list_sessions(idle_prune_seconds):
            counts[s.status] += 1
        return dict(counts)


# ---------------------------------------------------------------------------
#  Alert engine (bounded, edge-triggered)
# ---------------------------------------------------------------------------
@dataclass
class Alert:
    severity: str   # INFO | WARNING | CRITICAL
    key: str
    message: str
    ts: str


class AlertLog:
    def __init__(self, maxlen: int = 100):
        self._events: deque[Alert] = deque(maxlen=maxlen)
        self._firing: set[str] = set()
        self._lock = threading.Lock()

    def evaluate(self, conditions: list[tuple[str, str, str]]) -> list[dict]:
        """conditions: list of (key, severity, message) currently TRUE.
        Appends an event only on a rising edge (new key). Returns the recent log."""
        now_keys = {k for k, _, _ in conditions}
        with self._lock:
            for key, severity, message in conditions:
                if key not in self._firing:
                    self._events.appendleft(
                        Alert(severity=severity, key=key, message=message,
                              ts=datetime.now(timezone.utc).isoformat())
                    )
            self._firing = now_keys
            return [a.__dict__ for a in list(self._events)[:30]]


# ---------------------------------------------------------------------------
#  History sampler (bounded ring for the live chart)
# ---------------------------------------------------------------------------
class HistorySampler:
    def __init__(self, maxlen: int = 480):
        self._samples: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, sample: dict) -> None:
        with self._lock:
            self._samples.append(sample)

    def recent(self, seconds: int) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [s for s in self._samples if s["_ts"] >= cutoff]


# ---------------------------------------------------------------------------
#  Central registry (singleton)
# ---------------------------------------------------------------------------
class Telemetry:
    def __init__(self) -> None:
        self.http = RollingWindow()
        self.openai = ProviderMetrics("openai")
        self.elevenlabs = ProviderMetrics("elevenlabs")
        self.live = LiveRegistry()
        self.alerts = AlertLog()
        self.history = HistorySampler()
        self.http_in_flight = Gauge()
        self.interview_in_flight = Gauge()
        self.interview_waiting = Gauge()          # requests currently waiting for a slot
        self.interview_wait = RollingWindow()      # wait-time samples + "timeout" counter
        self.tts_in_flight = Gauge()
        self.assessment_in_flight = Gauge()
        self.started_at = time.time()
        self._sampler_stop: threading.Event | None = None
        self._sampler_thread: threading.Thread | None = None

    # --- HTTP recording (from middleware) ---
    def record_http(self, status_code: int, latency_ms: float) -> None:
        self.http.incr("requests")
        self.http.observe_latency(latency_ms)
        if status_code >= 500:
            self.http.incr("5xx")
        elif status_code == 429:
            self.http.incr("429")
            self.http.incr("4xx")
        elif status_code >= 400:
            self.http.incr("4xx")
        else:
            self.http.incr("2xx")

    def uptime_seconds(self) -> int:
        return int(time.time() - self.started_at)

    # --- background sampler for the live chart ---
    def start_sampler(self, interval_seconds: int, active_window_seconds: int) -> None:
        if self._sampler_thread is not None:
            return
        self._sampler_stop = threading.Event()

        def _loop():
            while not self._sampler_stop.wait(interval_seconds):
                try:
                    self.history.record({
                        "_ts": time.time(),
                        "t": datetime.now(timezone.utc).isoformat(),
                        "active_users": self.live.active_users(active_window_seconds),
                        "http_rpm": self.http.rate_per_min("requests", 60),
                        "openai_rpm": self.openai.window.rate_per_min("requests", 60),
                        "elevenlabs_rpm": self.elevenlabs.window.rate_per_min("requests", 60),
                        "rate_limited": self.http.sum("429", 60),
                    })
                except Exception:  # never let the sampler kill the process
                    pass

        self._sampler_thread = threading.Thread(target=_loop, name="telemetry-sampler", daemon=True)
        self._sampler_thread.start()

    def stop_sampler(self) -> None:
        if self._sampler_stop:
            self._sampler_stop.set()
        self._sampler_thread = None


_telemetry: Telemetry | None = None


def get_telemetry() -> Telemetry:
    global _telemetry
    if _telemetry is None:
        _telemetry = Telemetry()
    return _telemetry


def reset_telemetry() -> None:
    """Test helper - rebuild the registry."""
    global _telemetry
    if _telemetry is not None:
        _telemetry.stop_sampler()
    _telemetry = Telemetry()
