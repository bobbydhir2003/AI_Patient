"""Self-contained REAL local end-to-end check for Priority J.

Starts the backend as a subprocess, seeds a super-admin, then drives an actual
SIMULATED_AI load test through the live HTTP API (separate worker process) and
prints the real measured metrics + capacity analysis. Used to validate the
feature locally; not part of the pytest suite.
"""
import os
import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
DB = "/tmp/lt_live.db"
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{DB}",
        "AUTO_CREATE_TABLES": "true",
        "ENVIRONMENT": "development",
        "MOCK_AI": "false",
        "RATE_LIMIT_ENABLED": "false",
        "LOGIN_THROTTLE_ENABLED": "false",
        "OPENAI_PATIENT_STREAMING_ENABLED": "false",
        "BACKGROUND_WORKERS_ENABLED": "true",
        "ASSESSMENT_QUEUE_ENABLED": "false",
        "JWT_SECRET": "local-dev-secret-key-at-least-32-bytes-long-xx",
        "LOAD_TEST_TARGET_BASE_URL": BASE,
        "MOCK_MODEL_LATENCY_MS": "40",
    }
    print("[driver] launching uvicorn…", flush=True)
    uvlog = open("/tmp/uv_driver.log", "w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=BACKEND, env=env, stdout=uvlog, stderr=uvlog,
    )
    client = httpx.Client(base_url=BASE, timeout=15.0, trust_env=False)
    try:
        # wait for health
        for _ in range(40):
            print("[driver] waiting for health…", flush=True)
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("SERVER DID NOT START"); return 1

        # seed super-admin directly via raw sqlite3 + bcrypt (NO heavy app import
        # in this driver process, so only uvicorn holds the app in memory).
        import sqlite3
        import uuid
        from datetime import datetime, timezone
        import bcrypt
        pw = bcrypt.hashpw(b"pw12345678", bcrypt.gensalt()).decode()
        now = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(DB)
        try:
            exists = con.execute("SELECT 1 FROM users WHERE email='super@live.edu'").fetchone()
            if not exists:
                con.execute(
                    "INSERT INTO users (id,email,password_hash,full_name,student_number,role,"
                    "account_status,is_active,is_load_test,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "super@live.edu", pw, "S", "", "super_admin",
                     "ACTIVE", 1, 0, now, now),
                )
                con.commit()
        finally:
            con.close()

        tok = client.post("/api/auth/login",
                          json={"email": "super@live.edu", "password": "pw12345678"}).json()["accessToken"]
        H = {"Authorization": f"Bearer {tok}"}

        # RBAC quick check with a plain (no-token) request
        assert client.get("/api/admin/system/load-tests/config").status_code == 401

        job = client.post("/api/admin/system/load-tests", headers=H, json={
            "testType": "smoke", "providerMode": "SIMULATED_AI",
            "targetUsers": 10, "rampSeconds": 2, "durationSeconds": 8,
        }).json()
        jid = job["id"]
        print(f"[create] job={jid} status={job['status']} provider={job['providerMode']} users={job['targetUsers']}")

        last = None
        for _ in range(40):
            time.sleep(1.0)
            m = client.get(f"/api/admin/system/load-tests/{jid}/metrics", headers=H).json()
            last = m
            ov = m.get("overall") or {}
            live = m.get("live")
            print(f"[poll] live={live} status={m['job']['status']} "
                  f"requests={ov.get('requests')} success%={ov.get('successRate')} "
                  f"activeSamples={len(m.get('series') or [])}")
            if not live:
                break

        cap = (last or {}).get("capacity") or {}
        ov = (last or {}).get("overall") or {}
        print("\n===== REAL RESULT =====")
        print("final status:", last["job"]["status"])
        print("requests:", ov.get("requests"), "success%:", ov.get("successRate"),
              "p95ms:", (ov.get("latencyMs") or {}).get("p95"))
        print("statusCounts:", ov.get("statusCounts"))
        print("capacity.overallStatus:", cap.get("overallStatus"))
        print("safeCapacity:", cap.get("recommendedSafeCapacity"))
        print("bottleneck:", cap.get("observedBottleneck"))
        tele = (last or {}).get("telemetry") or {}
        print("providers.openai rpm:", (tele.get("providers", {}).get("openai") or {}).get("requests_per_minute"))
        print("infra.server available:", (tele.get("infrastructure", {}).get("server") or {}).get("available"))

        # recent no longer empty
        recent = client.get("/api/admin/system/load-tests/recent", headers=H).json()
        print("recent runs:", [(x["id"][:8], x["status"]) for x in recent["jobs"]])
        ok = last["job"]["status"] in ("COMPLETED",) and (ov.get("requests") or 0) > 0
        print("\nCHECK:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        client.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
