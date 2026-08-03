#!/usr/bin/env python3
"""Load/capacity-test worker (Priority J) - runs in a SEPARATE process.

This process is launched by the controller (app/services/load_test_service.py)
via `python -m load_tests.worker ...`. It NEVER runs inside the FastAPI event
loop. It drives a realistic virtual-student workflow against a running backend
and emits REAL measured telemetry as JSON snapshots that the controller reads
back for the live dashboard and the final persisted summary.

Every number this worker writes is measured from actual HTTP responses. It
never fabricates latency, throughput, capacity, or provider usage. Provider
Activity / infrastructure health are merged in by the controller from the
backend's own real telemetry - not invented here.

Isolation: virtual students authenticate ONLY as dedicated, pre-provisioned
`is_load_test` accounts (credentials passed in via --credentials-file). Real
student accounts are never touched.

Workflow per virtual student (repeats until the run deadline):
  login (once) -> GET /api/cases -> POST /api/sessions -> N x
  POST /api/interviews/{sid}/messages (think-time between) ->
  [optional] POST /api/voice/synthesize (TTS traffic) ->
  [optional] POST /api/sessions/{sid}/complete ->
  [optional] POST /api/sessions/{sid}/assessment (+ poll).

Snapshot file (atomic write to <metrics-dir>/<job-id>.json) contains:
  status, startedAt, elapsed, activeUsers, targetUsers, cumulative totals,
  a bounded down-sampled time-series of 1s windows, and (on exit) a final
  summary with overall percentiles. Raw per-request data is never persisted.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone


# --------------------------------------------------------------------------
#  Percentile helper (nearest-rank)
# --------------------------------------------------------------------------
def pctile(data: list[float], p: float) -> float | None:
    if not data:
        return None
    s = sorted(data)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return round(s[k], 1)


# --------------------------------------------------------------------------
#  Thread-safe measured metrics
# --------------------------------------------------------------------------
class Metrics:
    """Accumulates REAL request outcomes. Overall latency uses a bounded
    reservoir so a 60-min soak cannot exhaust memory; per-window latencies are
    reset each sample for live percentiles."""

    RESERVOIR = 10000

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # cumulative
        self.requests = 0
        self.success = 0          # 2xx/3xx
        self.failed = 0           # >=400
        self.network_errors = 0   # no HTTP response at all
        self.status_counts: dict[int, int] = {}
        # overall latency reservoir (ms) + seen count for reservoir sampling
        self.reservoir: list[float] = []
        self._seen = 0
        # interview-turn latency reservoir (ms)
        self.turn_latencies: list[float] = []
        # concurrency
        self.active_users = 0
        self.max_active_users = 0
        # per-window (reset each sample)
        self.w_requests = 0
        self.w_success = 0
        self.w_failed = 0
        self.w_latencies: list[float] = []

    def user_start(self):
        with self.lock:
            self.active_users += 1
            self.max_active_users = max(self.max_active_users, self.active_users)

    def user_end(self):
        with self.lock:
            self.active_users -= 1

    def record(self, status: int, ms: float, *, interview: bool = False):
        with self.lock:
            self.requests += 1
            self.w_requests += 1
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            if 200 <= status < 400:
                self.success += 1
                self.w_success += 1
            else:
                self.failed += 1
                self.w_failed += 1
            self.w_latencies.append(ms)
            self._reservoir_add(ms)
            if interview and 200 <= status < 400:
                if len(self.turn_latencies) < self.RESERVOIR:
                    self.turn_latencies.append(ms)

    def record_network_error(self):
        with self.lock:
            self.requests += 1
            self.w_requests += 1
            self.network_errors += 1
            self.failed += 1
            self.w_failed += 1

    def _reservoir_add(self, ms: float):
        self._seen += 1
        if len(self.reservoir) < self.RESERVOIR:
            self.reservoir.append(ms)
        else:
            j = random.randint(0, self._seen - 1)
            if j < self.RESERVOIR:
                self.reservoir[j] = ms

    def snapshot_window(self, interval_s: float) -> dict:
        """Read + reset the per-window accumulators; returns a live sample."""
        with self.lock:
            reqs, succ, fail = self.w_requests, self.w_success, self.w_failed
            lat = self.w_latencies
            self.w_requests = self.w_success = self.w_failed = 0
            self.w_latencies = []
            active = self.active_users
            cum = dict(requests=self.requests, success=self.success,
                       failed=self.failed, networkErrors=self.network_errors)
        rps = round(reqs / interval_s, 2) if interval_s > 0 else 0.0
        sr = round(succ / reqs * 100, 2) if reqs else None
        return {
            "activeUsers": active,
            "windowRequests": reqs,
            "requestsPerSec": rps,
            "windowSuccess": succ,
            "windowFailed": fail,
            "successRate": sr,
            "p50": pctile(lat, 50),
            "p95": pctile(lat, 95),
            "p99": pctile(lat, 99),
            "cumulative": cum,
        }

    def overall(self) -> dict:
        with self.lock:
            total = self.requests
            return {
                "requests": total,
                "success": self.success,
                "failed": self.failed,
                "networkErrors": self.network_errors,
                "successRate": round(self.success / total * 100, 2) if total else None,
                "statusCounts": {str(k): v for k, v in sorted(self.status_counts.items())},
                "maxActiveUsers": self.max_active_users,
                "latencyMs": {
                    "p50": pctile(self.reservoir, 50),
                    "p95": pctile(self.reservoir, 95),
                    "p99": pctile(self.reservoir, 99),
                },
                "turnLatencyMs": {
                    "p50": pctile(self.turn_latencies, 50),
                    "p95": pctile(self.turn_latencies, 95),
                    "p99": pctile(self.turn_latencies, 99),
                },
            }


# --------------------------------------------------------------------------
#  Atomic snapshot writer (bounded, down-sampled series)
# --------------------------------------------------------------------------
MAX_SERIES_POINTS = 300  # persisted/live series is bounded; never unbounded raw


class SnapshotWriter:
    def __init__(self, path: str, meta: dict):
        self.path = path
        self.meta = meta
        self.series: list[dict] = []

    def add_sample(self, sample: dict):
        self.series.append(sample)
        # Down-sample in place if we exceed the cap: keep every other point.
        if len(self.series) > MAX_SERIES_POINTS:
            self.series = self.series[::2]

    def write(self, *, status: str, started_at: str | None, elapsed: float,
              target_users: int, overall: dict | None, error: str | None = None,
              final: dict | None = None):
        payload = {
            **self.meta,
            "status": status,
            "startedAt": started_at,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "elapsedSeconds": round(elapsed, 1),
            "targetUsers": target_users,
            "series": self.series,
            "overall": overall,
            "error": error,
        }
        if final is not None:
            payload["final"] = final
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)  # atomic on POSIX
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------
#  One virtual student
# --------------------------------------------------------------------------
def run_student(cfg, creds: dict, metrics: Metrics, stop: threading.Event, deadline: float, http):
    """Login once, then loop the student workflow until the deadline/stop."""
    import httpx  # local import so --help works without deps

    client = httpx.Client(base_url=cfg.base_url, timeout=60.0, trust_env=False)
    metrics.user_start()
    try:
        def call(method, path, *, json_body=None, headers=None, interview=False):
            t0 = time.monotonic()
            try:
                r = client.request(method, path, json=json_body, headers=headers)
                metrics.record(r.status_code, (time.monotonic() - t0) * 1000, interview=interview)
                return r
            except Exception:
                metrics.record_network_error()
                return None

        # --- authenticate (pre-provisioned load-test account) ---
        lr = call("POST", "/api/auth/login",
                  json_body={"email": creds["email"], "password": creds["password"]})
        if lr is None or lr.status_code != 200:
            return
        token = lr.json().get("accessToken")
        if not token:
            return
        hdr = {"Authorization": f"Bearer {token}"}

        while not stop.is_set() and time.monotonic() < deadline:
            # browse catalog (a real student loads cases first)
            call("GET", "/api/cases", headers=hdr)

            s = call("POST", "/api/sessions",
                     json_body={"studentName": creds.get("name", "Load Test"),
                                "caseId": cfg.case_id}, headers=hdr)
            if s is None or s.status_code != 201:
                if stop.wait(0.5):
                    return
                continue
            sid = s.json()["sessionId"]

            for t in range(cfg.turns):
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                tid = f"{sid}-{t}-{random.randint(0, 1_000_000)}"
                mr = call("POST", f"/api/interviews/{sid}/messages",
                          json_body={"text": _prompt(t), "caseId": cfg.case_id,
                                     "clientTurnId": tid, "source": "typed"},
                          headers=hdr, interview=True)
                # optional REAL TTS traffic
                if cfg.enable_tts and mr is not None and mr.status_code == 200:
                    body = mr.json()
                    ptext = (body.get("patientText") or "").strip()
                    turn_id = body.get("turnId") or ""
                    if ptext:
                        call("POST", "/api/voice/synthesize",
                             json_body={"caseId": cfg.case_id, "text": ptext,
                                        "sessionId": sid, "turnId": turn_id},
                             headers=hdr)
                # think time (with jitter) - keeps the workflow realistic
                if cfg.think_time_ms:
                    jitter = cfg.think_time_ms * (0.5 + random.random())
                    if stop.wait(jitter / 1000.0):
                        break

            if cfg.complete:
                call("POST", f"/api/sessions/{sid}/complete", headers=hdr)

            if cfg.assessment and not stop.is_set():
                sub = call("POST", f"/api/sessions/{sid}/assessment", headers=hdr)
                if sub is not None and sub.status_code in (200, 201, 202):
                    t_start = time.monotonic()
                    while time.monotonic() - t_start < cfg.assessment_timeout_s:
                        if stop.is_set() or time.monotonic() >= deadline:
                            break
                        st = call("GET", f"/api/sessions/{sid}/assessment/status", headers=hdr)
                        if st is None:
                            break
                        status = (st.json() or {}).get("status")
                        if status in ("completed", "failed"):
                            break
                        if stop.wait(1.0):
                            break
    finally:
        metrics.user_end()
        client.close()


def _prompt(turn_index: int) -> str:
    prompts = [
        "Can you tell me what brings you in today?",
        "How long have you been feeling this way?",
        "Does anything make it better or worse?",
        "How is this affecting your daily activities?",
        "Have you noticed any other symptoms?",
        "What are you most concerned about?",
        "Have you tried anything to manage it so far?",
        "Is there anything else you'd like me to know?",
    ]
    return prompts[turn_index % len(prompts)]


# --------------------------------------------------------------------------
#  Driver: ramp -> hold, sampling once per second
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="PT AI load/capacity-test worker (separate process).")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--metrics-dir", required=True)
    ap.add_argument("--credentials-file", required=True,
                    help="JSON list of {email,password[,name]} for pre-provisioned is_load_test users.")
    ap.add_argument("--base-url", default=os.environ.get("LOAD_TEST_TARGET_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--target-users", type=int, required=True)
    ap.add_argument("--ramp-seconds", type=int, default=0)
    ap.add_argument("--duration-seconds", type=int, required=True)
    ap.add_argument("--turns", type=int, default=int(os.environ.get("TURN_COUNT", 4)))
    ap.add_argument("--case-id", default=os.environ.get("CASE_ID", "carly"))
    ap.add_argument("--think-time-ms", type=int, default=int(os.environ.get("THINK_TIME_MS", 800)))
    ap.add_argument("--test-type", default="smoke")
    ap.add_argument("--provider-mode", default="SIMULATED_AI")
    ap.add_argument("--enable-tts", action="store_true")
    ap.add_argument("--assessment", action="store_true")
    ap.add_argument("--complete", action="store_true")
    ap.add_argument("--assessment-timeout-s", type=int, default=60)
    ap.add_argument("--sample-interval-s", type=float, default=1.0)
    cfg = ap.parse_args()

    with open(cfg.credentials_file) as f:
        creds_list = json.load(f)
    if not creds_list:
        raise SystemExit("No load-test credentials provided; cannot run.")

    metrics = Metrics()
    stop = threading.Event()

    def _handle_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    meta = {
        "jobId": cfg.job_id,
        "testType": cfg.test_type,
        "providerMode": cfg.provider_mode,
        "environment": "local",
        "baseUrl": cfg.base_url,
        "rampSeconds": cfg.ramp_seconds,
        "durationSeconds": cfg.duration_seconds,
        "ttsEnabled": bool(cfg.enable_tts),
    }
    path = os.path.join(cfg.metrics_dir, f"{cfg.job_id}.json")
    writer = SnapshotWriter(path, meta)

    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    end_time = t0 + cfg.ramp_seconds + cfg.duration_seconds
    writer.write(status="RUNNING", started_at=started, elapsed=0.0,
                 target_users=cfg.target_users, overall=metrics.overall())

    # Spawn virtual students, staggered across the ramp window.
    threads: list[threading.Thread] = []
    n = cfg.target_users
    for i in range(n):
        creds = creds_list[i % len(creds_list)]
        delay = (i / n) * cfg.ramp_seconds if (cfg.ramp_seconds > 0 and n > 0) else 0.0

        def _runner(cr=creds, d=delay):
            if d and stop.wait(d):
                return
            if stop.is_set() or time.monotonic() >= end_time:
                return
            run_student(cfg, cr, metrics, stop, end_time, None)

        th = threading.Thread(target=_runner, daemon=True)
        threads.append(th)
        th.start()

    # Sampler loop (main thread): one measured sample per interval.
    next_sample = t0 + cfg.sample_interval_s
    error = None
    try:
        while time.monotonic() < end_time and not stop.is_set():
            time.sleep(min(0.2, cfg.sample_interval_s))
            now = time.monotonic()
            if now >= next_sample:
                sample = metrics.snapshot_window(cfg.sample_interval_s)
                sample["t"] = round(now - t0, 1)
                writer.add_sample(sample)
                writer.write(status="RUNNING", started_at=started, elapsed=now - t0,
                             target_users=cfg.target_users, overall=metrics.overall())
                next_sample += cfg.sample_interval_s
    except Exception as exc:  # unexpected worker failure
        error = f"{type(exc).__name__}: {exc}"

    # Signal all students to stop and drain briefly.
    stop.set()
    for th in threads:
        th.join(timeout=10.0)

    overall = metrics.overall()
    elapsed = time.monotonic() - t0
    final = {
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "wallTimeSeconds": round(elapsed, 1),
        "overall": overall,
        "samplesCollected": len(writer.series),
    }
    status = "FAILED" if error else "COMPLETED"
    writer.write(status=status, started_at=started, elapsed=elapsed,
                 target_users=cfg.target_users, overall=overall, error=error, final=final)


if __name__ == "__main__":
    main()
