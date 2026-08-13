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
        # TTS-specific counters + latency reservoir (streaming_voice mode)
        self.tts_requests = 0
        self.tts_success = 0
        self.tts_failed = 0
        self.tts_slot_timeouts = 0    # HTTP 409 from /voice/synthesize = degraded/browser fallback
        self.tts_latencies: list[float] = []
        # per-turn outcome (streaming_voice mode: did the SSE stream reach "final"?)
        self.completed_turns = 0
        self.failed_turns = 0
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

    def record_tts(self, status: int | None, ms: float):
        """One /voice/synthesize call (streaming_voice mode). `status` is None
        for a network-level failure (counted as failed, not a slot timeout)."""
        with self.lock:
            self.tts_requests += 1
            if status is not None and 200 <= status < 400:
                self.tts_success += 1
                if len(self.tts_latencies) < self.RESERVOIR:
                    self.tts_latencies.append(ms)
            else:
                self.tts_failed += 1
                if status == 409:  # TTS concurrency slot exhausted -> browser fallback
                    self.tts_slot_timeouts += 1

    def record_turn_outcome(self, ok: bool):
        with self.lock:
            if ok:
                self.completed_turns += 1
            else:
                self.failed_turns += 1

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
                # Convenience call-outs for the codes operators watch most
                # closely (also derivable from statusCounts above).
                "http409Count": self.status_counts.get(409, 0),
                "http429Count": self.status_counts.get(429, 0),
                "http5xxCount": sum(v for k, v in self.status_counts.items() if k >= 500),
                "maxActiveUsers": self.max_active_users,
                "latencyMs": {
                    "p50": pctile(self.reservoir, 50),
                    "p95": pctile(self.reservoir, 95),
                    "p99": pctile(self.reservoir, 99),
                },
                # OpenAI-backed request latency (the interview /messages call,
                # or the SSE stream call in streaming_voice mode).
                "turnLatencyMs": {
                    "p50": pctile(self.turn_latencies, 50),
                    "p95": pctile(self.turn_latencies, 95),
                    "p99": pctile(self.turn_latencies, 99),
                },
                "ttsRequests": self.tts_requests,
                "ttsSuccess": self.tts_success,
                "ttsFailed": self.tts_failed,
                "ttsSlotTimeouts": self.tts_slot_timeouts,
                "ttsDegraded": self.tts_slot_timeouts,
                "ttsLatencyMs": {
                    "p50": pctile(self.tts_latencies, 50),
                    "p95": pctile(self.tts_latencies, 95),
                    "p99": pctile(self.tts_latencies, 99),
                },
                "completedTurns": self.completed_turns,
                "failedTurns": self.failed_turns,
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


# --------------------------------------------------------------------------
#  One virtual student - REALISTIC STREAMING VOICE mode (Issue 6)
# --------------------------------------------------------------------------
# The classic run_student() above sends one bulk POST /messages and (if TTS is
# on) ONE full-response TTS call per turn - that proves the backend can answer
# messages, not that real students with voice actually work. This mode instead
# drives the SAME SSE endpoint + per-sentence TTS pattern the real frontend
# uses: stream the patient reply, and for EACH emitted sentence issue exactly
# one TTS request, SEQUENTIALLY (never several in flight for one student) -
# then wait for it before moving to the next sentence, exactly like a browser
# playing audio clips back to back.
def _parse_sse_block(lines: list[str]) -> tuple[str, str]:
    event = ""
    data = ""
    for line in lines:
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()
    return event, data


def run_student_streaming_voice(cfg, creds: dict, metrics: Metrics, stop: threading.Event,
                                 deadline: float, http_client=None):
    """Login once, then loop: SSE-streamed patient replies with sequential
    per-sentence TTS, until the deadline/stop. `http_client`, when provided
    (tests), is used AS-IS instead of constructing a real one - this is what
    lets the sequential-per-sentence behavior be unit-tested against a
    MockTransport with no live server."""
    import httpx  # local import so --help works without deps

    client = http_client or httpx.Client(base_url=cfg.base_url, timeout=60.0, trust_env=False)
    owns_client = http_client is None
    metrics.user_start()
    try:
        def call(method, path, *, json_body=None, headers=None, interview=False, tts=False):
            t0 = time.monotonic()
            try:
                r = client.request(method, path, json=json_body, headers=headers)
                ms = (time.monotonic() - t0) * 1000
                metrics.record(r.status_code, ms, interview=interview)
                if tts:
                    metrics.record_tts(r.status_code, ms)
                return r
            except Exception:
                metrics.record_network_error()
                if tts:
                    metrics.record_tts(None, 0.0)
                return None

        lr = call("POST", "/api/auth/login",
                  json_body={"email": creds["email"], "password": creds["password"]})
        if lr is None or lr.status_code != 200:
            return
        token = lr.json().get("accessToken")
        if not token:
            return
        hdr = {"Authorization": f"Bearer {token}"}

        while not stop.is_set() and time.monotonic() < deadline:
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
                t0 = time.monotonic()
                got_final = False
                try:
                    with client.stream(
                        "POST", f"/api/interviews/{sid}/messages/stream",
                        json={"text": _prompt(t), "caseId": cfg.case_id,
                              "clientTurnId": tid, "source": "typed"},
                        headers=hdr,
                    ) as resp:
                        status = resp.status_code
                        if status != 200:
                            metrics.record(status, (time.monotonic() - t0) * 1000, interview=True)
                        else:
                            block: list[str] = []
                            for line in resp.iter_lines():
                                if line != "":
                                    block.append(line)
                                    continue
                                if not block:
                                    continue
                                event, data = _parse_sse_block(block)
                                block = []
                                if event == "sentence" and cfg.enable_tts:
                                    try:
                                        sentence_text = json.loads(data).get("text", "")
                                    except (json.JSONDecodeError, TypeError):
                                        sentence_text = ""
                                    if sentence_text:
                                        # ONE sequential TTS request per sentence -
                                        # exactly how the real frontend plays audio;
                                        # never more than one in flight per student.
                                        call("POST", "/api/voice/synthesize",
                                             json_body={"caseId": cfg.case_id, "text": sentence_text,
                                                        "sessionId": sid, "turnId": ""},
                                             headers=hdr, tts=True)
                                elif event == "final":
                                    got_final = True
                                elif event == "error":
                                    got_final = False
                            metrics.record(status, (time.monotonic() - t0) * 1000, interview=True)
                except Exception:
                    metrics.record_network_error()
                metrics.record_turn_outcome(got_final)

                if cfg.think_time_ms:
                    jitter = cfg.think_time_ms * (0.5 + random.random())
                    if stop.wait(jitter / 1000.0):
                        break

            if cfg.complete:
                call("POST", f"/api/sessions/{sid}/complete", headers=hdr)
            # Assessment generation is intentionally NOT triggered here - this
            # mode measures interview+TTS capacity, not assessment load (see
            # _derive_worker_params: assessment stays False for streaming_voice).
    finally:
        metrics.user_end()
        if owns_client:
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
    ap.add_argument(
        "--streaming-voice", action="store_true",
        help="Realistic voice-capacity mode: SSE-streamed replies + one sequential "
             "TTS call per emitted sentence, instead of one bulk /messages call.",
    )
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
        "streamingVoice": bool(cfg.streaming_voice),
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
            if cfg.streaming_voice:
                run_student_streaming_voice(cfg, cr, metrics, stop, end_time)
            else:
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
