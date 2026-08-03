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

A per-process bounded semaphore (`MAX_CONCURRENT_AI_INTERVIEWS`, default 20)
gates OpenAI interview generation. Flow: a request tries to reserve a slot; if
none is free it waits up to `AI_INTERVIEW_WAIT_SECONDS`; if still none, it gets a
controlled **503 `service_overloaded`** (with `Retry-After`) — never a raw OpenAI
error. Slots are always released (success or exception). The dashboard shows
`active / limit`.

## 5. TTS protection

A separate semaphore (`MAX_CONCURRENT_TTS_REQUESTS`) gates ElevenLabs. TTS is
**best-effort**: if no slot is free within `TTS_WAIT_SECONDS`, the request
**degrades to browser text-to-speech** (HTTP 409 the frontend already handles) —
the interview turn is never failed because audio is saturated. Audio ordering is
preserved by the existing per-sentence pipeline.

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

**Per worker (in-memory):** active users, HTTP counters/latency, in-flight gauges,
OpenAI/ElevenLabs counters, live-session registry, alerts, history chart. With N
uvicorn workers, each has its own copy — the dashboard shows the worker that
served the request. The Priority A **rate limiter and login throttle are also
process-local**.

## 10. Which metrics are global / database-backed

**Shared across workers (from the database):** assessment queue depth
(`pending`/`processing`), oldest wait, and anything derived from `assessment_runs`.
The DB pool stats are per-worker (each worker has its own pool) but reflect real
PostgreSQL connections.

## 11. Current deployment limitations

- **Single service / process-local telemetry.** Numbers describe one worker. For
  a true fleet-wide view you need shared state.
- **Rate limits are per worker.** Effective limit ≈ configured × worker count.
- **No circuit breaker yet** — telemetry + retry/backoff cover current failure
  patterns; a breaker can be added later if provider outages become common.
- **CPU/memory require `psutil`** (now in requirements). Without it those cards
  show "Not available" rather than a fake value.
- **SQLite (local dev)** has no connection pool; the DB Pool card reports that
  honestly.

## 12. What would require Redis / shared state

- **Global rate limiting / login throttle** across workers and instances.
- **Fleet-wide active-user and concurrency counts** (single source of truth).
- **A cross-instance job queue** (the current DB-backed queue already works for a
  single service; Redis/Celery would help at higher assessment volume or multi-
  instance execution).
Redis is intentionally **not** added now — the app handles its current single-
service deployment safely without it.

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

# Terminal 2 - load stages (start small!)
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 5  --sessions 10 --turns 6
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 --concurrency 20 --sessions 20 --turns 6
```

Do **not** run the 50/70-user stages against real providers. For a controlled
real-provider test, use a small `--sessions` count, `MOCK_AI=false`,
`ENABLE_TTS=false`, and watch the dashboard + provider bills.
