#!/usr/bin/env python3
"""Safe, configurable load-test tool for the PT AI Patient Simulator (Priority B).

It simulates realistic student behavior (register → login → start case → send
interview turns with think-time → complete → submit assessment → poll result)
against a running backend, and reports latency percentiles, error/429/5xx rates,
interview response time, max concurrency and assessment completion time.

SAFETY:
- Defaults to MOCK_AI mode expectations: point it at a server started with
  MOCK_AI=true so it spends NO OpenAI/ElevenLabs credits.
- ENABLE_TTS defaults to false (TTS costs money on a real provider).
- It NEVER runs the 50/70-user stages automatically; you pass CONCURRENCY.

Usage:
  python -m load_tests.loadtest \
      --base-url http://127.0.0.1:8000 --concurrency 5 --sessions 20 --turns 6

Environment overrides: BASE_URL, CONCURRENCY, SESSION_COUNT, TURN_COUNT,
ENABLE_TTS, THINK_TIME_MS, CASE_ID, ASSESSMENT, ASSESSMENT_TIMEOUT_S.
"""
from __future__ import annotations

import argparse
import os
import queue
import statistics
import threading
import time
import uuid

import httpx


# --------------------------------------------------------------------------
#  Shared metrics
# --------------------------------------------------------------------------
class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: list[float] = []
        self.interview_latencies: list[float] = []
        self.assessment_times: list[float] = []
        self.status_counts: dict[int, int] = {}
        self.errors = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def start_req(self):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def end_req(self, status: int, ms: float, *, interview=False):
        with self.lock:
            self.in_flight -= 1
            self.latencies.append(ms)
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            if interview and 200 <= status < 300:
                self.interview_latencies.append(ms)

    def record_error(self):
        with self.lock:
            self.errors += 1

    def record_assessment(self, seconds: float):
        with self.lock:
            self.assessment_times.append(seconds)


def pctile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return round(s[k], 1)


# --------------------------------------------------------------------------
#  One virtual-user session
# --------------------------------------------------------------------------
def run_session(cfg, metrics: Metrics, idx: int) -> None:
    client = httpx.Client(base_url=cfg.base_url, timeout=60.0, trust_env=False)
    try:
        email = f"loadtest+{uuid.uuid4().hex[:12]}@example.edu"

        def call(method, path, *, json=None, headers=None, interview=False):
            metrics.start_req()
            t0 = time.monotonic()
            status = 0
            try:
                r = client.request(method, path, json=json, headers=headers)
                status = r.status_code
                return r
            except Exception:
                metrics.record_error()
                return None
            finally:
                metrics.end_req(status, (time.monotonic() - t0) * 1000, interview=interview)

        # register + login
        reg = call("POST", "/api/auth/register", json={
            "fullName": f"Load Test {idx}", "email": email, "password": "loadtest123", "studentNumber": f"LT{idx}",
        })
        if reg is None or reg.status_code != 201:
            return
        token = reg.json()["accessToken"]
        hdr = {"Authorization": f"Bearer {token}"}

        # start case
        s = call("POST", "/api/sessions", json={"studentName": "LT", "caseId": cfg.case_id}, headers=hdr)
        if s is None or s.status_code != 201:
            return
        sid = s.json()["sessionId"]

        # interview turns with think-time
        for t in range(cfg.turns):
            call("POST", f"/api/interviews/{sid}/messages",
                 json={"text": f"Question {t + 1}: how are you feeling today?", "caseId": cfg.case_id, "clientTurnId": f"{sid}-{t}"},
                 headers=hdr, interview=True)
            if cfg.think_time_ms:
                time.sleep(cfg.think_time_ms / 1000.0)

        # complete
        call("POST", f"/api/sessions/{sid}/complete", headers=hdr)

        # assessment (queued) + poll to completion
        if cfg.assessment:
            sub = call("POST", f"/api/sessions/{sid}/assessment", headers=hdr)
            if sub is not None and sub.status_code in (200, 201, 202):
                t_start = time.monotonic()
                while time.monotonic() - t_start < cfg.assessment_timeout_s:
                    st = call("GET", f"/api/sessions/{sid}/assessment/status", headers=hdr)
                    if st is None:
                        break
                    status = (st.json() or {}).get("status")
                    if status == "completed":
                        metrics.record_assessment(time.monotonic() - t_start)
                        break
                    if status == "failed":
                        break
                    time.sleep(1.0)
    finally:
        client.close()


# --------------------------------------------------------------------------
#  Driver
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("CONCURRENCY", 5)))
    ap.add_argument("--sessions", type=int, default=int(os.environ.get("SESSION_COUNT", 20)))
    ap.add_argument("--turns", type=int, default=int(os.environ.get("TURN_COUNT", 6)))
    ap.add_argument("--case-id", default=os.environ.get("CASE_ID", "carly"))
    ap.add_argument("--think-time-ms", type=int, default=int(os.environ.get("THINK_TIME_MS", 200)))
    ap.add_argument("--assessment", default=os.environ.get("ASSESSMENT", "true").lower() == "true")
    ap.add_argument("--assessment-timeout-s", type=int, default=int(os.environ.get("ASSESSMENT_TIMEOUT_S", 60)))
    ap.add_argument("--enable-tts", default=os.environ.get("ENABLE_TTS", "false").lower() == "true")
    cfg = ap.parse_args()

    metrics = Metrics()
    work: "queue.Queue[int]" = queue.Queue()
    for i in range(cfg.sessions):
        work.put(i)

    def worker():
        while True:
            try:
                idx = work.get_nowait()
            except queue.Empty:
                return
            try:
                run_session(cfg, metrics, idx)
            finally:
                work.task_done()

    print(f"== Load test: {cfg.concurrency} concurrent · {cfg.sessions} sessions · {cfg.turns} turns/case ==")
    print(f"   target={cfg.base_url}  case={cfg.case_id}  assessment={cfg.assessment}  tts={cfg.enable_tts}")
    t0 = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(cfg.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0

    total = sum(metrics.status_counts.values())
    ok = sum(c for s, c in metrics.status_counts.items() if 200 <= s < 400)
    c429 = metrics.status_counts.get(429, 0)
    c5xx = sum(c for s, c in metrics.status_counts.items() if s >= 500)
    c503 = metrics.status_counts.get(503, 0)

    print("\n---------------- RESULTS ----------------")
    print(f"wall_time_s          : {elapsed:.1f}")
    print(f"requests             : {total}")
    print(f"success_rate         : {(ok / total * 100 if total else 0):.2f}%  ({ok}/{total})")
    print(f"network_errors       : {metrics.errors}")
    print(f"status_counts        : {dict(sorted(metrics.status_counts.items()))}")
    print(f"429_rate_limited     : {c429}")
    print(f"503_overload         : {c503}")
    print(f"5xx                  : {c5xx}")
    print(f"throughput_req_s     : {(total / elapsed if elapsed else 0):.1f}")
    print(f"max_concurrent_reqs  : {metrics.max_in_flight}")
    print("-- latency (all requests, ms) --")
    print(f"  p50 / p95 / p99    : {pctile(metrics.latencies, 50)} / {pctile(metrics.latencies, 95)} / {pctile(metrics.latencies, 99)}")
    if metrics.latencies:
        print(f"  avg / max          : {statistics.mean(metrics.latencies):.1f} / {max(metrics.latencies):.1f}")
    print("-- interview turn latency (ms) --")
    print(f"  p50 / p95 / p99    : {pctile(metrics.interview_latencies, 50)} / {pctile(metrics.interview_latencies, 95)} / {pctile(metrics.interview_latencies, 99)}")
    if metrics.interview_latencies:
        print(f"  avg                : {statistics.mean(metrics.interview_latencies):.1f}")
    if metrics.assessment_times:
        print("-- assessment completion (s) --")
        print(f"  count / p50 / p95  : {len(metrics.assessment_times)} / {pctile(metrics.assessment_times, 50)} / {pctile(metrics.assessment_times, 95)}")
    else:
        print("-- assessment completion : none completed within timeout --")
    print("-----------------------------------------")


if __name__ == "__main__":
    main()
