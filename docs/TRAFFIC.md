# Traffic Control & Scalability (Priority B)

This document explains the traffic-control layer added in Priority B: what each
metric means, how protections work, and — importantly — the honest limits of
what the current single-service deployment can and cannot measure or control.

The guiding rule: **every number on the Traffic Dashboard is measured.** If a
value cannot be measured in the current deployment, the dashboard shows
"Not available" / "Not applicable" / "Single instance" rather than a fake number.

---

## 1. What each metric means

| Metric | Meaning | Source |
|---|---|---|
| **Active Users** | Distinct authenticated users with API activity in the last `ACTIVE_USER_WINDOW_SECONDS` | In-memory registry (per process) |
| **Live Interviews** | Interview sessions currently in a non-finished state | In-memory live-session registry |
| **Requests / min** | HTTP requests/min across all routes | Rolling counter (per process) |
| **In flight** | HTTP requests currently being handled | Gauge (per process) |
| **OpenAI active / req-min / p95 / 429 / retries** | Live OpenAI usage | Instrumented OpenAI client |
| **ElevenLabs active / req-min / p95 / 429** | Live TTS usage | Instrumented ElevenLabs client |
| **Assessment queued / processing / oldest wait** | Background assessment queue depth | Indexed query on `assessment_runs` |
| **CPU / Memory** | Process/host CPU and system memory | `psutil` (if installed) |
| **DB Pool** | Checked-out / size / overflow | SQLAlchemy pool (PostgreSQL only) |
| **p50 / p95 / p99 latency** | HTTP latency percentiles (last 5 min) | Bounded latency reservoir |
| **Uptime** | Seconds since this worker started | Process clock |

## 2. How "active users" are calculated

A user is **active** if we have seen an authenticated request from them within
`ACTIVE_USER_WINDOW_SECONDS` (default 120s). The telemetry middleware decodes the
bearer token (no DB hit) and records `user_id -> last_seen` in an in-memory map;
stale entries are pruned on read. This is **not** "logged in" — a user with a
valid token who is idle is not counted. It is intentionally activity-based.

## 3. Request rate vs concurrency

- **Rate** (requests/min) = how many requests arrive over time. High rate with
  fast responses is fine.
- **Concurrency** (in-flight, AI active) = how many are being processed *at once*.
  Concurrency is what exhausts CPU, DB connections and provider capacity. The
  concurrency guards (below) bound concurrency, not rate.

## 4. Interview concurrency protection

A bounded semaphore (`MAX_CONCURRENT_AI_INTERVIEWS`, default 20) gates OpenAI
interview generation. Flow: a request tries to reserve a slot; if none is free
it waits up to `AI_INTERVIEW_WAIT_SECONDS`; if still none, it gets a
controlled **503 `service_overloaded`** (with `Retry-After`) — never a raw OpenAI
error. Slots are always released (success or exception). The dashboard shows
`active / limit`.

**GLOBAL when Redis is configured** (`REDIS_URL`, required in production/
staging — see `docs/DEPLOYMENT.md`): the semaphore is backed by
`core/distributed_semaphore.py`, so with N uvicorn workers the configured
limit is the true fleet-wide cap, not N x the limit. Without Redis (local
dev/test only), it falls back to the original per-process semaphore.

## 5. TTS protection

A separate semaphore (`MAX_CONCURRENT_TTS_REQUESTS`, default 10, sized for the
planned `eleven_flash_v2_5` setup) gates ElevenLabs — **never shared with the
OpenAI semaphore**. TTS is **best-effort**: if no slot is free within
`TTS_WAIT_SECONDS` (including when Redis is required but unreachable — treated
identically to "no slot available"), the request **degrades to browser
text-to-speech** (HTTP 409 the frontend already handles) — the interview turn
is never failed because audio is saturated. Audio ordering is preserved by the
existing per-sentence pipeline. Also global when Redis is configured (see §4).

## 6. How assessment queuing works

Assessment makes several OpenAI calls (30–120s), so it must not run in the web
request. On submit, the backend creates an `assessment_runs` row with status
**PENDING** and returns `202` immediately. A pool of `ASSESSMENT_WORKER_CONCURRENCY`
(default 3) background threads claims PENDING runs atomically (SELECT + conditional
UPDATE), executes the pipeline, and stores the result. The frontend polls the
existing status endpoint. Because the queue *is* the database table, a process
restart does not lose jobs (a run stuck in PROCESSING from a crash is re-reaped
after 10 min). Live interviews keep OpenAI priority because their concurrency
limit is larger and independent.

**Duplicate prevention:** a partial unique index (`uq_active_assessment_per_session`
on `status IN ('PENDING','PROCESSING','VERIFYING')`) makes it impossible to have
two active runs for one session — a concurrent double-submit collapses to one run
via an IntegrityError that returns the existing run. No double spend.

**Fleet-wide execution cap:** the DB-backed claim (SELECT + conditional UPDATE)
is already safe with multiple worker processes — only one worker's UPDATE can
flip a given row. What was NOT safe with multiple workers was the *in-flight
cap*: each process independently allowing up to `ASSESSMENT_WORKER_CONCURRENCY`
concurrent jobs would multiply to (workers x concurrency) fleet-wide, which is
exactly the "many students finish together" flood this queue exists to
prevent. This is now bounded by the same `DistributedSemaphore` mechanism used
for OpenAI/TTS (a separate named limit, `"assessment"`, never shared with
either), so the configured concurrency is the true fleet-wide cap when Redis
is configured.

## 7. What 429 means

`429 Too Many Requests` is returned by our **rate limiter** (Priority A) when an
identity/IP exceeds its configured request rate. Separately, `429` *from a
provider* (OpenAI/ElevenLabs) is a provider rate limit; those are retried with
backoff (below) and counted in provider telemetry. A `503 service_overloaded` is
different: it means our interview concurrency guard is full.

## 8. What each alert means

Alerts are threshold-driven and edge-triggered (logged once when a condition
starts), bounded to the last ~100 events.

| Alert | Severity | Condition |
|---|---|---|
| API p95 latency high | WARNING | p95 > `ALERT_P95_LATENCY_MS` |
| API error rate high | WARNING | 5xx rate > `ALERT_ERROR_RATE` |
| OpenAI/ElevenLabs 429s | WARNING | provider 429s in last 5 min |
| AI concurrency high | WARNING | interview slots ≥ `ALERT_AI_CONCURRENCY_PCT` of limit |
| Assessment queue deep | WARNING | pending > `ALERT_ASSESSMENT_QUEUE` |
| TTS unavailable | INFO | ElevenLabs failing; text-only fallback active |
| CPU/Memory critical | CRITICAL | ≥ `ALERT_CPU_PCT` / `ALERT_MEMORY_PCT` |
| DB pool critical | CRITICAL | pool utilization ≥ `ALERT_DB_POOL_PCT` |

## 9. Which metrics are process-local

**Per worker (in-memory):** active users, HTTP counters/latency, in-flight
gauges, OpenAI/ElevenLabs RPM/TPM/429/latency *counters*, live-session
registry, alerts, history chart. With N uvicorn workers, each has its own copy
— the dashboard shows the worker that served the request. The Priority A
**rate limiter and login throttle are also process-local** (unchanged — still
an accepted tradeoff; effective limit ≈ configured × worker count).

Note the distinction: the OpenAI/ElevenLabs **telemetry counters** above
(RPM/TPM/429s/latency, used for dashboards and the adaptive-throttle capacity
state) are still process-local. The **concurrency admission control**
(in-flight/active counts and the actual "am I allowed to make this call right
now" decision) is a separate mechanism — see §10.

## 10. Which metrics are global / database-backed

**Shared across workers (from the database):** assessment queue depth
(`pending`/`processing`), oldest wait, and anything derived from `assessment_runs`.
The DB pool stats are per-worker (each worker has its own pool) but reflect real
PostgreSQL connections.

**Shared across workers (from Redis, when `REDIS_URL` is configured):** the
OpenAI interview, TTS, and assessment-execution **concurrency limits**
(`core/distributed_semaphore.py`) — the "active" count on the Traffic
Dashboard's Interview/TTS concurrency cards reports the real fleet-wide count
(`active_scope: "global"`) rather than one worker's slice
(`active_scope: "process"`) whenever Redis is connected.

## 11. Current deployment limitations

- **Provider RPM/TPM telemetry and the adaptive assessment-throttle capacity
  state are still process-local**, even with Redis configured (only the
  concurrency *admission* is global — see §10). Each worker's view of "how
  busy is OpenAI" is only its own slice of traffic; with 4 workers the
  BUSY/PROTECTING/CRITICAL thresholds can therefore be reached later than the
  fleet's true aggregate usage would suggest. A future iteration could move
  token/request counting into Redis too if this proves material in practice.
- **Rate limits and the login throttle are per worker.** Effective limit ≈
  configured × worker count (unchanged by this round of work).
- **No circuit breaker yet** — telemetry + retry/backoff cover current failure
  patterns; a breaker can be added later if provider outages become common.
- **CPU/memory require `psutil`** (already in requirements). Without it those
  cards show "Not available" rather than a fake value.
- **SQLite (local dev)** has no connection pool; the DB Pool card reports that
  honestly.

## 12. Redis / shared state

Redis is now used for **fleet-wide concurrency control** (§10) —
`REDIS_URL`, required in production/staging (see `docs/DEPLOYMENT.md`). It is
NOT used for caching, sessions, or general shared state. Still local/DB-backed
(possible future work, not part of this change):

- **Global rate limiting / login throttle** across workers and instances.
- **Fleet-wide OpenAI RPM/TPM telemetry** (see §11).
- **Fleet-wide active-user counts** (single source of truth).

## 13. What would require AWS autoscaling later

Horizontal scaling (multiple app instances behind an ALB with an autoscaling
group) is a deployment-phase change, not an app change. The dashboard deliberately
does **not** display desired/in-service/standby instance counts or an autoscaling
toggle, because the app cannot (and should not) query AWS for that. When
autoscaling is introduced, add shared state (Redis) first so the process-local
metrics above become fleet-wide.

---

## Load testing (mock mode)

`backend/load_tests/loadtest.py` drives realistic student flows. Point it at a
server started with `MOCK_AI=true` to spend **zero** OpenAI/ElevenLabs credits:

```bash
# Terminal 1 - mock server (no credits spent)
MOCK_AI=true MOCK_MODEL_LATENCY_MS=200 BACKGROUND_WORKERS_ENABLED=true \
ASSESSMENT_QUEUE_ENABLED=true RATE_LIMIT_ENABLED=false LOGIN_THROTTLE_ENABLED=false \
DATABASE_URL=sqlite:////tmp/ptai_load.db AUTO_CREATE_TABLES=true ENVIRONMENT=development \
uvicorn app.main:app --port 8099

# Terminal 2 - staged load test (start small!). LOAD_TEST_MAX_USERS now
# allows up to 170 (the scalability target - see docs/DEPLOYMENT.md).
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 20  --sessions 20  --turns 6
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 70  --sessions 70  --turns 6
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 100 --sessions 100 --turns 6
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 130 --sessions 130 --turns 6
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 170 --sessions 170 --turns 6
```

Do **not** run the 70+ user stages against real providers. For a controlled
real-provider test, use a small `--sessions` count, `MOCK_AI=false`,
`ENABLE_TTS=false`, and watch the dashboard + provider bills. Do not describe
the system as supporting 170 concurrent students until a run at that level
actually comes back PASS or PASS_WITH_WARNING from
`app/services/load_capacity_analysis.py` — use "designed for / being
validated for 170 concurrent students" until then.
