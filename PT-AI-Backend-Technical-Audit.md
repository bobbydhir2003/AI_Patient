# PT AI Patient Simulator — Complete Backend Technical Audit

**Audit type:** Read-only architecture + scalability audit (no code was modified).
**Basis:** The actual source under `backend/app/**` and `src/**` was read directly. Where documentation and code disagree, the code wins and the disagreement is called out. Anything I could not confirm from code is marked **NOT VERIFIED**.

A quick, important framing before the details: **this is a genuinely well-engineered codebase.** The hard scalability problems (holding DB connections during provider calls, per-worker concurrency multiplication, double-spend on assessments, retry storms, honest telemetry) have mostly already been thought about and solved in code. The real risks that remain are (1) a mismatch between the *deployed* runtime and the *designed* runtime, and (2) a small number of specific, verifiable issues. I'll be precise about which is which.

---

## PART 1 — Repository Map (what actually exists)

```
PT-AI-Patient-Dual-Assessment/
├── backend/
│   ├── app/
│   │   ├── main.py                     FastAPI app factory + lifespan + telemetry middleware
│   │   ├── api/                         HTTP routers (all sync `def` handlers)
│   │   │   ├── auth, students, sessions, interviews, queue, voice,
│   │   │   ├── assessments, cases, health, access
│   │   │   └── admin, admin_system, admin_runtime, admin_traffic,
│   │   │       admin_usage, admin_load_tests, admin_users
│   │   ├── core/                        cross-cutting infrastructure
│   │   │   ├── config.py                Pydantic settings (fail-closed prod rules)
│   │   │   ├── concurrency.py           interview_slot / tts_slot context managers
│   │   │   ├── distributed_semaphore.py Redis ZSET+Lua fleet-wide semaphore (+ local fallback)
│   │   │   ├── redis_client.py          shared sync redis-py client, ping cache
│   │   │   ├── worker_registry.py       Redis heartbeat → observed fleet
│   │   │   ├── telemetry.py             in-memory, per-process metrics
│   │   │   ├── capacity.py              OpenAI TPM/RPM capacity state (process-local)
│   │   │   ├── assessment_worker.py     DB-backed background assessment thread pool
│   │   │   ├── provider_retry.py        classify + backoff retry wrapper
│   │   │   ├── rate_limit.py            process-local fixed-window limiter + login throttle
│   │   │   ├── crypto.py, security.py   password hash, JWT, config encryption
│   │   │   └── exceptions.py, constants.py, pricing.py, logging.py
│   │   ├── database/
│   │   │   ├── connection.py            engine + session factory + get_db / get_db_factory
│   │   │   └── migrations/versions/     Alembic 0001 … 0018
│   │   ├── models/                      SQLAlchemy ORM (users, sessions, turns, assessments,
│   │   │                                ai_usage_events, load_test_jobs, runtime_config …)
│   │   ├── repositories/                thin data-access (session, transcript, user, audit)
│   │   ├── services/                    business logic (interview, queue, session, system,
│   │   │                                traffic, load_test, usage, runtime_config, auth …)
│   │   ├── patient_engine/              OpenAI client, streaming engine, prompt builder,
│   │   │                                sentence stream, speaker router
│   │   ├── voice/                       ElevenLabs client, audio cache, voice profile loader
│   │   ├── assessment/                  standard + referral assessment pipelines, call budget
│   │   ├── cases/ , case_assessment/ , rubrics/ , prompts/   JSON case + rubric content
│   │   └── dependencies/auth.py         auth/ownership FastAPI dependencies
│   ├── load_tests/                      loadtest.py (dev CLI) + worker.py (Priority-J worker)
│   ├── scripts/start.sh                 container entrypoint (migrations → uvicorn)
│   ├── Dockerfile, requirements.txt, alembic.ini, .env.example
│   └── ptai.db                          (a local SQLite dev DB checked into the folder)
├── src/                                 React + TypeScript frontend (Vite)
│   ├── pages/InterviewPage.tsx, InterviewQueuePage.tsx, AssessmentLoadingPage.tsx …
│   ├── services/                        api.ts, patientStreamService.ts, patientVoiceService.ts,
│   │                                    queueApi.ts, systemApi.ts, trafficApi.ts, loadTestApi.ts
│   ├── hooks/useVoiceConversation.ts    mic / VAD / playback state machine
│   └── portal/                          admin portal UI
└── docs/DEPLOYMENT.md, docs/TRAFFIC.md  honest, accurate architecture docs
```

**Stack (from `requirements.txt` + code):** FastAPI + Uvicorn, SQLAlchemy 2.0 (sync), Alembic, `openai>=1.40` SDK, `httpx` (ElevenLabs), `redis>=5` (sync), PyJWT, bcrypt, psutil. Frontend: React + Vite + TypeScript.

---

## PART 2 — The real backend request flow

Your proposed diagram is *directionally* right but has three important corrections:

1. **There is no message broker / job queue in the live request path.** "Redis / Queue / Semaphore" is really *two different things*: a Redis-backed **semaphore** (concurrency admission) and a separate **waiting queue** for *starting* interviews. Neither is a Celery/RQ-style task queue. Assessments (not chat) use a **database table as the queue**.
2. **OpenAI and ElevenLabs are not sequential stages of one call.** A chat turn calls **OpenAI** to produce text; **ElevenLabs is a *separate* HTTP request** the browser makes afterward (`POST /api/voice/synthesize`), one per sentence. They are different endpoints.
3. **A "worker" is a Uvicorn process, and today there is exactly one.** Inside it, requests run on a **thread pool**, not on the async event loop.

Answers to your 10 questions, from the code:

1. **How a request reaches FastAPI** — Browser → (Nginx reverse proxy on the EC2 host, config lives outside the repo — see Part 7) → `127.0.0.1:8000` Uvicorn → the ASGI app built in `main.py:create_app()`. CORS + a telemetry middleware (`main.py:_install_telemetry_middleware`) wrap every request.
2. **How one worker is selected** — With a single Uvicorn process there is nothing to load-balance; the OS accepts the socket and Uvicorn's event loop dispatches it. With the *planned* 4 workers, Uvicorn's built-in multiprocess mode has all workers `accept()` on the shared socket — the **kernel** picks which worker wakes up. Nginx talks to one address; it does not know about individual workers.
3. **What a worker actually is** — one OS **process** running the whole FastAPI app with its own memory (its own telemetry counters, its own DB pool, its own rate-limit counters).
4. **One request or many?** — **Many, concurrently.** Every route handler in `app/api/*` is a **sync `def`** (I checked: zero `async def` handlers). FastAPI runs sync handlers in Starlette's **anyio thread pool** (default **40 threads**). So one worker handles up to ~40 requests being actively processed at once, plus more waiting for a thread.
5. **While waiting for OpenAI** — the calling **thread blocks** on the OpenAI SDK (`patient_engine/openai_client.py`, blocking `client.responses.create(...)`). The **event loop is *not* blocked** (this is the whole point of the sync-in-threadpool design), so other requests keep flowing. That thread is occupied until OpenAI responds.
6. **While waiting for ElevenLabs** — same: a thread blocks on `httpx.Client.stream(...)` in `voice/elevenlabs_client.py`. It is a *different request* (`/api/voice/synthesize`) than the chat turn.
7. **Do those block the worker?** — They block a **thread**, not the **event loop**. The worker keeps serving other requests up to the thread-pool limit. This is the correct pattern for blocking SDKs and is a real strength of the codebase.
8. **Where Redis participates** — only three things: fleet-wide **concurrency semaphores** (`distributed_semaphore.py`), the **interview waiting queue** (`services/interview_queue.py`), and **worker heartbeats** (`worker_registry.py`). **Not** caching, sessions, or rate limits. Redis is *optional in dev* and *required in prod/staging* (config fail-closed).
9. **Where PostgreSQL/SQLite participates** — everything durable: users, sessions, transcript turns, assessment runs + results, AI usage events, runtime config, load-test jobs. Prod = PostgreSQL; local dev/tests = SQLite (`backend/ptai.db`).
10. **How the response returns** — Non-streaming turn: one JSON `TurnResponse`. Streaming turn: **Server-Sent Events** (`text/event-stream`) with `speech` / `sentence` / `final` / `error` events. Audio: a streamed `StreamingResponse` of MP3 bytes from `/api/voice/synthesize`.

---

## PART 3 — The real architecture (ASCII)

```
                                   STUDENT BROWSER (React SPA)
                    typed/spoken question + separate per-sentence TTS calls
                                           │  HTTPS
                                           ▼
                                    NGINX (reverse proxy, on the EC2 host;
                                    config NOT in repo — external)
                                           │  proxy_pass 127.0.0.1:8000
                                           ▼
                          ┌───────────────────────────────────────┐
                          │  UVICORN — 1 worker today (docs plan 4) │
                          │  FastAPI app (app/main.py)              │
                          │  anyio THREADPOOL (default 40 threads)  │
                          │  every route handler is sync `def`      │
                          └───────────────────────────────────────┘
                                           │
      telemetry middleware (per-process, in-memory)  +  JWT auth (dependencies/auth.py)
                                           │
        ┌──────────────────────┬───────────┴───────────┬───────────────────────┐
        ▼                      ▼                        ▼                       ▼
  PostgreSQL (prod)       REDIS (prod-required)   OpenAI Responses API   ElevenLabs TTS
  / SQLite (dev)          ─ used ONLY for:         (blocking SDK call)   (blocking httpx
  SQLAlchemy pool         • ptai:sem:*  semaphores  gpt-4o-mini           stream, per
  pool_size=5             • ptai:iq:*   interview   text or SSE           sentence)
  max_overflow=10           waiting queue           streaming
  per worker              • ptai:worker:* heartbeat
        │                      │
        │        ┌─────────────┴───────────────────────────────┐
        │        │ CONCURRENCY ADMISSION (core/concurrency.py)  │
        │        │  interview_slot  → DistributedSemaphore       │
        │        │      "openai_interview"  limit=20             │
        │        │  tts_slot        → DistributedSemaphore        │
        │        │      "tts"               limit=15 (best-effort)│
        │        │  assessment      → DistributedSemaphore        │
        │        │      "assessment"        limit=3 (adaptive)    │
        │        └───────────────────────────────────────────────┘
        │
        ▼
  ASSESSMENT QUEUE = the `assessment_runs` TABLE (status PENDING→PROCESSING→COMPLETE)
  drained by a per-process background thread pool (core/assessment_worker.py),
  fleet-wide capped by the "assessment" semaphore, adaptively throttled by
  OpenAI capacity state (core/capacity.py). NOT Celery/Redis-queue.

  Retry: provider_retry.call_with_retry (exp backoff + jitter, 429/5xx/timeouts only)
  Rate limit: process-local fixed-window (core/rate_limit.py) — per worker
  Audio cache: in-memory LRU per process (voice/audio_cache.py)
  Load-test workers: SEPARATE OS processes (python -m load_tests.worker), driven by
       services/load_test_service.py; can flip a GLOBAL mock_ai DB flag.
```

**Streaming pipeline (one chat turn, streaming enabled):**

```
Browser ──POST /interviews/{sid}/messages/stream──▶ interviews.py
  → authorize_session_from_token (short DB session, closed)
  → interview_stream_service.stream_student_message
      → _load_context (short DB session #1: snapshot transcript, closed)
      → interview_slot().__enter__()      (acquire Redis semaphore BEFORE streaming)
      → StreamingResponse( _stream_events(...) )
          → streaming_engine.stream_patient_response
              → openai_client.stream_text  (SSE deltas from OpenAI)
              → sentence segmentation + validation
          → yield SSE: speech, sentence(0..n), final
          → _commit_turn (short DB session #2: 1 student + 1 patient turn, closed)
          → interview_slot released on every exit path (incl. client disconnect)
  Browser, per emitted sentence ──POST /voice/synthesize──▶ voice.py
      → _load_synth_context (short DB session, closed BEFORE provider call)
      → tts_slot().acquire()  (Redis semaphore; if full → 409 → browser TTS fallback)
      → ElevenLabs httpx stream → MP3 chunks streamed to browser
```

---

## PART 4 — Database audit

**Engine / config** (`database/connection.py`):
- Prod default `DATABASE_URL = postgresql+psycopg2://…` (`core/config.py`). Tests/dev use SQLite (`backend/ptai.db`).
- Pool (Postgres only): `pool_size=5`, `max_overflow=10`, `pool_recycle=1800s`, `pool_timeout=30s`, `pool_pre_ping=True`. SQLite path uses `check_same_thread=False` and **no pool** (honestly reported by the dashboard).
- Session factory: `autoflush=False, autocommit=False, expire_on_commit=False`. `expire_on_commit=False` is deliberate so ORM objects stay usable after commit (needed by the response serializers).
- **Two DI styles:** `get_db()` yields one request-scoped session (normal endpoints); `get_db_factory()` yields a *factory* so streaming endpoints can open **several short-lived** sessions instead of holding one open across a stream. This is the key scalability decision and it's implemented correctly.

**Migrations:** Alembic 0001→0018, applied by `scripts/start.sh` (`alembic upgrade head`) on container start. `auto_create_tables` (`Base.metadata.create_all`) exists but is a dev convenience; Alembic is the source of truth.

**Important tables**

| Table | Purpose | Important columns | Relationships / indexes |
|---|---|---|---|
| `students` | Roster identity | id, name, student_number, email, is_practice | 1‑N `interview_sessions` |
| `users` | Auth accounts | email, password_hash, role, student_id, is_active, account_status, is_load_test | N‑1 `students` |
| `interview_sessions` | One interview | id, student_id, case_id, status, locked, disclosed_fact_ids(JSON), config_snapshot(JSON), active_topic, started_at, completed_at, is_practice | idx student_id, case_id, is_practice |
| `conversation_turns` | Transcript | session_id, turn_index, role, speaker_id, content, client_turn_id, source, model_name, facts_used, validation_status | **uq(session_id,turn_index)**, **uq(session_id,client_turn_id)**, idx session_id |
| `assessment_runs` | Assessment job + result | session_id, status(PENDING/PROCESSING/…), overall_level, verification_status, error_code | idx status, **partial-unique uq_active_assessment_per_session** |
| `assessment_domain_results` / `assessment_evidence` | Rubric detail | performance_level, evidence excerpts | cascade from run |
| `ai_usage_events` | Cost/usage source of truth | provider, model, purpose, input/output/total_tokens, characters_generated, estimated_cost_usd, unit prices, created_at | idx (provider,created_at), (session_id,provider) |
| `load_test_jobs` | Priority-J jobs | test_type, provider_mode, target_users, status, results(JSON) | — |
| `runtime_config` (SystemSetting/ApiCredential/PatientVoiceSetting/ConfigurationHistory) | Live-editable config + encrypted keys | key/value(+type), encrypted credentials | — |
| `access_requests`, `audit_log` | Gated registration + audit | — | — |

**Data flows (verified in code):**

*Student login* — `POST /api/auth/login` → `services/auth_service` verifies bcrypt hash → `core/security` signs a JWT (HS256, `sub=user_id`, 12h). Login is IP+email brute-force throttled (`core/rate_limit._LoginThrottle`) and IP rate-limited.

*Starting an assessment/interview* — `POST /api/sessions` (`api/sessions.py` → `session_service.create_session`): owner is **always** the authenticated account's linked `Student` (never the request body — A3), a `config_snapshot` is frozen, one `interview_sessions` row is created. No AI call happens at session creation.

*Chat message* — see Parts 2/22. **DB writes:** exactly **one student turn + one (or two, for joint-speaker) patient turn** per message, committed once (`interview_service.send_student_message` / `interview_stream_service._commit_turn`). Then a best-effort `ai_usage_events` row per real OpenAI request. OpenAI is called; ElevenLabs is a *separate* later request.

*Assessment completion* — `POST /sessions/{id}/assessment` inserts an `assessment_runs` row `PENDING` and returns 202 immediately. The background worker flips it to `PROCESSING`, runs the pipeline (2–3 OpenAI calls, hard-capped), writes `assessment_domain_results` + `assessment_evidence`, and sets `COMPLETE`/`VERIFIED` (or `FAILED`/`NEEDS_REVIEW`). Frontend polls status every 2.5s.

---

## PART 5 — Database scalability findings

| Severity | File → symbol | Problem | Why it matters | 10 / 30 / 70 / 170 users |
|---|---|---|---|---|
| **LOW (mostly mitigated)** | `voice/api/interview_stream_service` | **DB session held during provider I/O** — *this is explicitly avoided.* Streaming + `/synthesize` open short sessions and close them before OpenAI/ElevenLabs calls. | The classic killer bug is already fixed. | Fine at all levels *for streaming/voice*. |
| **MEDIUM** | `interview_service.send_student_message` (non-streaming path) | The **non-streaming** turn holds the **request-scoped `get_db()` session open across the OpenAI call** (`with interview_slot(): generate…` then `db.commit()`), because the whole handler uses one session. | A connection is checked out for the full OpenAI latency (~1–3 s). With `pool_size+overflow=15` per worker, sustained non-streaming turns can exhaust the pool faster than streaming. | 10/30: fine. 70: possible pool pressure if streaming is OFF. 170 single-worker: pool contention likely if streaming OFF. **Streaming ON avoids this.** |
| **MEDIUM** | `database/connection.py` pool sizing vs `app_workers` | `pool_size=5 + max_overflow=10` **per worker**. Planned 4 workers × 15 = 60 connections; Postgres default `max_connections=100`. | Safe with 4 workers, but if workers are raised without re-checking Postgres, connections can be exhausted. | Only relevant once multi-worker; documented in DEPLOYMENT.md. |
| **LOW** | `runtime_config_service.mock_ai_enabled()` / `openai_runtime()` / `elevenlabs_runtime()` | Each call **opens its own short DB session** when none is passed (the provider clients call these with `db=None`). So each chat turn/TTS triggers a couple of extra tiny sessions/queries **outside** the request session. | Adds a few short pool checkouts per turn; individually cheap, but it's real extra DB traffic on the hot path. | Negligible ≤30; measurable but small at 70–170 (a few extra `SELECT system_settings` per turn). |
| **LOW** | `interview_queue.active_interview_count()` | Each queue `join`/`status` poll runs `COUNT(*)` on `interview_sessions` (+ `_avg_interview_minutes` runs a 20-row scan). | Only executes while students are *on the waiting screen*, every 3 s. Indexed, cheap. | Only matters if the queue is actually active (i.e. already at capacity). |
| **LOW** | Load tests writing rows | Simulated load tests create real `interview_sessions`, `conversation_turns`, `assessment_runs`, `ai_usage_events` for up to 170 virtual users, looping until the deadline. | A long soak can write **a lot** of throwaway rows under `is_load_test`/`is_practice`. Not a correctness bug; a housekeeping one. | 170-user soak = potentially tens of thousands of rows; plan cleanup. |

**Things I specifically looked for and did *not* find (good news):**
- **N+1 query loops:** none on the hot path. Transcript is fetched once per turn (`transcript_repo.list_turns`). Relationships are lazy but not iterated in loops on hot paths.
- **Transactions held open across OpenAI/ElevenLabs in the streaming/voice paths:** explicitly prevented (short sessions).
- **Connection leaks:** `get_db()` closes in `finally`; streaming/voice sessions close in `finally`; the background worker closes its session each loop.
- **Duplicate rows / double-submit:** prevented by `uq(session_id, client_turn_id)` on turns and the **partial-unique** `uq_active_assessment_per_session` on assessment runs (an IntegrityError collapses a double-submit to the existing run — no double spend).
- **SQLite in production:** config default is PostgreSQL; SQLite is dev/test only. A `backend/ptai.db` file is checked into the folder (dev artifact) — worth `.gitignore`-ing, but not a prod risk.

---

## PART 6 — Async / sync audit (this is the headline good news)

**Every FastAPI route handler in `app/api/*` is a synchronous `def`.** I verified there are **zero `async def` route handlers**. FastAPI therefore runs them in Starlette's **anyio worker thread pool**. The only `async` code is the app lifespan (starts background threads) and the telemetry middleware (`await call_next`, no blocking I/O). Consequently:

- **There is no "async endpoint calling a blocking library" bug** — the single most common FastAPI scalability trap. Blocking OpenAI SDK, blocking `httpx.Client`, sync SQLAlchemy, and sync `redis-py` are all the *correct* choice here *because* the handlers are sync and run off the event loop.

| Endpoint / service | async? | Blocking op inside? | Impact |
|---|---|---|---|
| `POST /interviews/{id}/messages` | sync `def` | OpenAI SDK (blocking), sync SQLAlchemy | Blocks **a thread**, not the loop. Correct. |
| `POST /interviews/{id}/messages/stream` | sync `def` → sync SSE generator | OpenAI streaming (blocking iter), 2 short DB sessions | Blocks a thread while streaming. Correct. |
| `POST /voice/synthesize` | sync `def` → sync byte generator | ElevenLabs `httpx` stream (blocking) | Blocks a thread during synth. Correct. |
| `POST /sessions/{id}/assessment` | sync `def` | DB insert only (202 fast return) | Trivial; real work is off-request in the worker thread pool. |
| queue join/status, health, auth | sync `def` | small DB queries / Redis | Trivial. |
| `core/*` semaphores, retry, rate limit | sync | `time.sleep` for backoff/poll (blocking **by design**) | Runs inside threadpool threads; fine. |

**The one real consequence:** the concurrency ceiling of a single worker is the **anyio thread pool size**, which is **not raised anywhere** (I grepped — no `RunVar`/`total_tokens`/`to_thread` tuning), so it's the **default 40**. Under heavy simultaneous streaming + TTS, blocked provider threads consume that pool. Interview semaphore 20 + TTS 15 = up to 35 threads potentially parked on provider I/O at once — close to 40. When the pool saturates, *cheap* endpoints (login, `/cases`, queue polling) start queuing behind provider calls.

**"What happens to other students if one request blocks?"** — A single blocked request only consumes one thread; others are unaffected **until** ~40 threads are simultaneously blocked. At that point new requests wait for a free thread and *everyone's* latency rises. This is the real single-worker limiter, and it's why the multi-worker plan matters. **Recommendation (not applied):** either run the planned 4 workers, or raise the anyio pool (`anyio.to_thread` limiter) with eyes open — but only *after* raising the provider semaphores if you want more real concurrency.

---

## PART 7 — Worker architecture

**Your mental model ("Nginx → Worker 1/2/3") is the *planned* design, not the *current* one.**

- **What is a worker?** A full copy of the app running as its own OS process, with its own memory, DB pool, telemetry, and rate-limit counters.
- **How does Nginx pick a worker?** It doesn't. Nginx proxies to **one** address (`127.0.0.1:8000`). With multiple Uvicorn workers, **Uvicorn** (built-in multiprocess, no Gunicorn) has all workers share the listen socket and the **kernel** distributes accepted connections. Nginx is unaware of worker count.
- **Show the actual deployment config:**
  - `scripts/start.sh`: `exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}` — **no `--workers` flag ⇒ a single worker.**
  - `Dockerfile`: `CMD ["sh","scripts/start.sh"]` — same single worker.
  - `docs/DEPLOYMENT.md`: *current = 1 worker, no Redis*; *planned = 4 workers + Redis* via a systemd unit `--workers 4`. **No systemd unit and no Nginx config exist in the repo** (they live on the server). So the repo, as shipped, boots **one Uvicorn worker**.
  - **NOT VERIFIED:** the actual production Nginx config and whether the server currently runs 1 or 4 workers. From the repo alone, the launch command is single-worker.
- **Can one worker handle multiple students?** Yes — up to ~40 concurrent in-flight requests (thread pool), gated further by the provider semaphores (20 interview / 15 TTS).
- **What determines capacity, and what's the *current* limiter?**
  - Ordered by how soon they bite on the **current single-worker** box: **(1) OpenAI/ElevenLabs latency × the concurrency semaphores** (20 / 15) → then **(2) the 40-thread anyio pool** → then **(3) CPU/RAM of a t3.micro (~1 GB)** → then **(4) DB pool** (15) → external provider RPM/TPM limits last.
  - **Most likely current limiter:** the **single worker + t3.micro memory** combined with **provider latency holding threads/semaphore slots**. The semaphores are *protective* (they convert overload into clean 503/queue/te​xt-fallback), so the *felt* limit is "slots fill → students queue or get browser voice," not a crash.

---

## PART 8 — Redis audit

**Why Redis exists:** exactly three fleet-wide coordination jobs, nothing else (no caching, no sessions, no rate limits). From `core/redis_client.py` docstring and usages:

```
Redis keys actually used:
├── ptai:sem:openai_interview     ZSET  → interview concurrency semaphore (lease-scored)
├── ptai:sem:tts                  ZSET  → TTS concurrency semaphore
├── ptai:sem:assessment           ZSET  → assessment execution semaphore
├── ptai:iq:queue                 ZSET  → interview waiting queue (score = join time, FIFO)
├── ptai:iq:reserved              ZSET  → transient admission reservations (score = expiry)
├── ptai:iq:student               HASH  → student_id → entry_id (one entry per person)
├── ptai:iq:entry:<id>            HASH  → per-waiter {student_id, case_id, joined_at, last_seen}
└── ptai:worker:<host>:<pid>      STRING(JSON) → per-worker heartbeat, TTL ~12s
```

- **TTLs:** semaphore tokens self-expire via a per-token score (`redis_semaphore_lease_seconds=180s`) so a crashed holder can't leak a slot forever; the ZSET key is `PEXPIRE`'d on each acquire. Worker heartbeat records expire at `worker_heartbeat_ttl_seconds=12s`. Queue entries are pruned after `ENTRY_TTL=30s` without a poll; reservations after `RESERVATION_TTL=120s`.
- **Locks/semaphores:** the acquire is a **single atomic Lua script** (`_ACQUIRE_SCRIPT`): prune expired tokens (`ZREMRANGEBYSCORE`), `ZCARD`, admit only if `count < capacity`. This is a correct distributed counting semaphore.
- **Rate limits / concurrency counters:** rate limits are **NOT** in Redis (they're process-local — `core/rate_limit.py`). Concurrency counters *are* Redis (the ZSET cardinality).
- **Caching:** none in Redis. The audio cache is in-process (`voice/audio_cache.py`).

**What happens when Redis goes down? — this is the most important safety question, and the code makes a *deliberate, split* decision:**

- **Concurrency admission = FAIL CLOSED** (option A/partial-F, not "bypass"). In `distributed_semaphore._acquire_redis`, when `redis_required` is true (prod/staging default) and Redis is unreachable, `acquire()` returns `None` — treated exactly like "no capacity."
  - For **interviews**: `interview_slot.__enter__` raises `ServiceOverloadedError` → clean **503 + Retry-After**. Interviews are **refused**, not flooded. So the answer to "could Redis failure overload the backend?" is **no** — the opposite: it *stops* new AI work.
  - For **TTS**: `tts_slot.acquire()` returns not-ok → the endpoint returns **409 → browser TTS fallback**. Voice degrades to robotic browser voice; the interview text still works.
  - For **assessments**: the claim semaphore returns `None` → the worker simply doesn't claim new jobs until Redis returns (jobs stay `PENDING` in the DB, not lost).
- **In dev (`redis_required=false`)**: it transparently **falls back to a per-process semaphore** (option F, partial). Safe for a single worker; unsafe if you run multiple workers without Redis (documented).
- **The `try/except: continue`-style "fail open" pattern you asked me to hunt for exists only where it's *safe*:** heartbeat writes, usage recording, telemetry, and queue pruning swallow Redis errors (they must never break a turn). None of those swallow-and-continue paths bypass concurrency control. **I found no place where a Redis failure silently removes the concurrency cap in production.**

**Bottom line:** Redis down in prod = **interviews return 503, voice degrades to browser TTS, assessments pause** — a safe, degraded mode, *not* an overload or crash. The only real availability cost is that at-capacity is reached "immediately" the instant Redis blips (a required-and-down Redis reads as zero capacity). See Part 17-A.

---

## PART 9 — Concurrency control (the real algorithm, with config values)

Two never-shared semaphores plus a third for assessments (`core/concurrency.py`, `core/distributed_semaphore.py`, `core/config.py`):

- **Interview (OpenAI):** limit `MAX_CONCURRENT_AI_INTERVIEWS=20`, bounded wait `AI_INTERVIEW_WAIT_SECONDS=2.0s`, then **503**.
- **TTS (ElevenLabs):** limit `MAX_CONCURRENT_TTS_REQUESTS=15` (`.env.example` shows 10; `config.py` default is 15 — a **doc/code mismatch**, code wins), bounded wait `TTS_WAIT_SECONDS=5.0s`, then **409 → browser voice**.
- **Assessment:** limit `ASSESSMENT_WORKER_CONCURRENCY=3`, non-blocking claim, adaptively throttled to 2/1/0 as OpenAI capacity gets BUSY/PROTECTING/CRITICAL.

**Simulation: 70 students each send a chat message at the same instant** (assuming streaming ON, single worker, Redis up):

1. **70 HTTPS requests** arrive at Nginx → Uvicorn (1 worker).
2. The worker dispatches handlers onto the **40-thread anyio pool**. ~40 begin executing; ~30 wait for a thread.
3. Each executing turn calls `interview_slot()` → Redis Lua acquire against limit **20**. **20 acquire a slot** and call OpenAI. The rest **poll every 50 ms up to 2 s** (`_POLL_INTERVAL_SECONDS`, `ai_interview_wait_seconds`).
4. As each of the 20 OpenAI calls finishes (typically ~1–3 s to first token / completion), it **releases** its slot; a waiting request grabs it. Requests that never get a slot within 2 s get a **clean 503 `service_overloaded` + Retry-After** — never a raw provider error.
5. **OpenAI** therefore sees **at most 20 concurrent** requests from this backend (the whole reason the semaphore exists), well under the `OPENAI_RPM_LIMIT=3000 / TPM=250000` tier.
6. **ElevenLabs** is hit *separately*, per sentence, as each streamed reply produces sentences — gated by the **TTS=15** semaphore. Overflow (16th+ concurrent sentence) gets **409 → browser voice** rather than failing the turn.
7. Net felt experience at 70 simultaneous sends on one worker: the first ~20 get AI immediately; the next ~20 wait ≤2 s; beyond that some see a **503 "at capacity, retry"** — *unless* they entered through the **interview waiting queue** (Part 10), which converts that into a position instead of a 503.

Key correctness details I verified: slots are released on **every** exit path including client disconnect and exceptions (`interview_slot.__exit__`, `tts_slot.release`, the streaming `finally`), so **no slot leak** on interruption; and the semaphore is **global** across workers when Redis is on, so 4 workers still means 20 total, not 80.

---

## PART 10 — Queue system (what's a real queue vs not)

There are **two** distinct "queues," and only one is a real queue:

1. **Interview waiting queue — a REAL queue** (`services/interview_queue.py`). Backed by a **Redis sorted set** `ptai:iq:queue` (FIFO by join time) + per-entry hashes, with an **in-process dict fallback** for single-worker dev. It gates *admission to starting a new interview* when `active_interview_count(DB) + reservations ≥ MAX_CONCURRENT_AI_INTERVIEWS`. Students poll `/api/queue/status/{id}` every 3 s; when they reach the front **and** a slot is free, they're admitted with a 120 s reservation to cover the "admitted → session created" gap.
2. **Semaphore waiting — NOT a queue.** The `interview_slot`/`tts_slot` "wait" is just a **50 ms poll loop up to a bounded timeout** inside the acquiring thread. There's no ordering, no fairness, no stored list — first thread to win the Lua `ZCARD < capacity` check wins. Callers waiting here are **open HTTP requests occupying a thread**, not entries in a store.
3. **Assessment "queue" — a DATABASE TABLE, not a broker.** `assessment_runs` rows with `status=PENDING`, drained by background threads via atomic SELECT+conditional UPDATE.
4. **Frontend "queue screen"** (`InterviewQueuePage.tsx`) — just UI polling #1.
5. **Load-test "queue"** — a Python in-memory work queue inside the load generator process; unrelated to production traffic.

**Answers:**
- **Where are waiting requests physically stored?** Interview-admission waiters: **Redis sorted set** (prod) or a **Python dict** (single-worker dev). Semaphore waiters: **nowhere** — they're live threads/open sockets polling. Assessment jobs: **the `assessment_runs` DB table**. HTTP request waiting for a thread: **the anyio pool's internal queue** (transient, in-memory).
- **If the backend restarts while students are waiting?** Assessment jobs **survive** (they're DB rows; a `PROCESSING` job orphaned by a crash is reaped back to `PENDING` after 10 min). Interview-queue entries in **Redis survive** a backend restart (they're in Redis, self-expiring after 30 s of no polling); in the **dev in-process** fallback they're **lost**. Semaphore waiters and in-flight streams are **dropped** (their HTTP requests die); the browser retries idempotently via `client_turn_id`.
- **If one worker dies?** Its in-flight requests fail; the browser retries with the same `client_turn_id` (idempotent — no double save/charge). Held semaphore slots **self-expire** after the 180 s lease, so a dead worker doesn't leak capacity. Assessment jobs it was running get **reaped** back to `PENDING`.
- **Can another worker resume a waiting request?** For **assessments: yes** — any worker's background pool can claim a `PENDING`/reaped run. For a **live chat/voice request: no** — an HTTP stream is bound to the process serving it; "resume" is the browser re-submitting (idempotent), not a worker handoff.

---

## PART 11 — OpenAI audit (`patient_engine/openai_client.py`)

- **Model:** `gpt-4o-mini` (config `openai_model`, live-overridable via runtime config). **API:** the **Responses API** with strict JSON-schema structured output for turns, and **streaming** text for the low-latency path.
- **Client init:** lazy `OpenAI(api_key, timeout)`, **cached and only rebuilt when key/timeout change** (fingerprint). Key + model + timeout are read **per request** from `runtime_config_service` so an admin can rotate the key/model live without a restart.
- **Timeout:** `openai_timeout_seconds=30`. **Token cap:** `openai_max_output_tokens=400` (patient replies use `openai_patient_max_output_tokens` if set).
- **Retries:** `provider_retry.call_with_retry` — exponential backoff + jitter, honors `Retry-After`, **retries only transient** (429/408/409/425/5xx/timeouts/connection), **never** 400/401/403/404/422 or validation. For **streaming**, only the **stream *start*** is retried (safe to replay); a **mid-stream failure is never retried** (would duplicate output). `provider_max_retries=3`.
- **Prompt construction:** `patient_engine/prompt_builder.py`. **Conversation-history size is bounded**: `actor_max_recent_turns=12` last turns + a secondary `actor_context_char_limit=12000` char cap. So you are **not** resending an unbounded transcript every turn.
- **Token accounting:** recorded **once per completed request** from **provider-reported `response.usage`** (non-streaming) or the streaming `response.completed` usage event — **never estimated from string length**, never per-chunk. Written to `ai_usage_events` via `usage_recorder`.
- **Rate-limit handling / key selection:** provider 429s are retried with backoff and counted; there is **no key pool/rotation** (single active key from runtime config). **Fallback:** if generation fails, **nothing is persisted** and the student keeps their question to retry (no fake patient replies).

**One full turn:** student message → prompt built from case facts + last-12 turns → `client.responses.create(...)` (streamed deltas or structured JSON) → sentence validation → provider usage read once → 1 patient turn saved → `ai_usage_events` row.

**Unexpectedly-high-token-usage causes — checked:**
- Whole conversation every turn? **No** — capped at 12 turns / 12k chars.
- Duplicated system prompts? Not observed; one developer prompt per call.
- Retrying completed requests? **No** — mid-stream/completed calls are never retried; only pre-first-token starts are.
- Multiple OpenAI calls per message? **One** per speaker segment (a rare joint "both" turn = 2). Assessments are separate and **hard-capped at 3** logical calls (`assessment_max_openai_calls`, enforced by `AssessmentCallBudget` — the module docstring notes this replaced an old "6–11 calls" runaway).
- Load tests hitting real OpenAI? **Only** if an admin explicitly chooses a REAL provider mode (capped at `load_test_real_provider_max_users=10` and requires confirmation). Default simulated mode spends nothing.
- Frontend duplicate submissions? Guarded by `client_turn_id` idempotency (unique index + in-flight ref in `InterviewPage.tsx`).

---

## PART 12 — ElevenLabs audit (`voice/elevenlabs_client.py`, `api/voice.py`)

- **When TTS is called:** the **browser** calls `POST /api/voice/synthesize` — **once per approved sentence** during streaming (the SSE `sentence` events), or once for a committed turn on the non-streaming path. It is **not** invoked server-side inside the chat turn.
- **Sentence-level pipelining:** **yes** — the streaming engine emits sentences as they arrive and the frontend/loadtest issues one TTS call per sentence.
- **Simultaneous TTS calls per student:** **one at a time.** The real frontend (and the `run_student_streaming_voice` load worker) issues per-sentence TTS **sequentially** — never several in flight for the same student. So *"if one reply has 5 sentences,"* that's **5 sequential ElevenLabs requests**, not 5 concurrent ones (per student).
- **Concurrency limit (all students):** `MAX_CONCURRENT_TTS_REQUESTS=15` via the `tts` semaphore; overflow → **409 → browser voice**. The shared `httpx.Client` pool is sized `max(15, elevenlabs_pool_min_connections=8)` so a request that won a semaphore slot never re-queues inside httpx.
- **Timeout/retries:** `elevenlabs_timeout_seconds=20`; connect-and-first-chunk retried on 429/5xx/timeout with backoff, **mid-stream failures not retried**. **Caching:** in-process LRU (`voice/audio_cache.py`, `elevenlabs_cache_max_entries=24`) keyed by voice+model+text+settings+format — a cache hit skips the provider and isn't billed.
- **Fallback:** any failure (semaphore full, provider error, connect failure) surfaces as a clean **409/502** the frontend already handles by switching to **browser (robotic) TTS**. The interview turn is never failed because audio is saturated.
- **Why voice breaks / goes robotic / delays under load:** the honest chain is — as concurrent students exceed **15 simultaneous sentence-synth requests fleet-wide**, the 16th+ gets a 409 within 5 s and the browser speaks it in a robotic local voice; if ElevenLabs itself returns 429/5xx or is slow, retries add latency and eventually degrade to browser voice. Because per-student TTS is sequential, a single student never self-saturates; saturation is a *fleet* effect. **The most likely voice-quality bottleneck at scale is this TTS semaphore (15) plus real ElevenLabs concurrency limits on your plan** (NOT VERIFIED — depends on your ElevenLabs tier).

---

## PART 13 — Retry-storm analysis

**Can the "slow → timeout → retry → double traffic → collapse" spiral happen? Largely no, by design:**

- **Server→provider retries are bounded and *reduce*, not amplify, pressure:** `provider_retry` retries only transient errors, honors `Retry-After`, uses exp backoff + jitter (`base=200ms`, `max=4000ms`, `provider_max_retries=3`). Crucially, retries happen **inside a held semaphore slot**, so retried calls **do not increase concurrency against the provider** — they occupy the *same* slot. That structurally prevents the "retries multiply concurrency" storm at the provider.
- **Overload is shed, not retried harder:** when the interview semaphore is full, the server returns **503 + Retry-After** *before* any provider call. This is load-shedding, the opposite of amplification.
- **Streaming mid-flight is never retried** (no duplicate generation).
- **Assessment jobs** back off adaptively as OpenAI capacity tightens (workers 3→2→1→0) and pause at CRITICAL — a built-in circuit-breaker-like behavior for the background path.

**Where a *client*-side storm could still originate (the residual risk):**
- The **frontend/browser** retry behavior on 503/timeout is the variable I can't fully bound from the backend. `InterviewPage.tsx` falls back from streaming→atomic **once** with the same idempotent `client_turn_id` (no duplicate save), which is well-behaved. **NOT VERIFIED:** whether any client auto-retries 503s aggressively without backoff. If it did, it would generate request pressure (more 503s) but **not** more provider calls (the semaphore still caps those). So worst case is a busy-loop of cheap 503s, not a provider meltdown.
- **No global circuit breaker** exists (documented in TRAFFIC.md §11) — acceptable given retries+shedding, but a provider outage means every turn pays up to 3 backoff attempts before failing. That's added latency, not a storm.

**Verdict:** retry amplification against OpenAI/ElevenLabs is structurally prevented by "retry inside the slot + shed on full." The only amplification vector is client-side request volume, which the backend absorbs as cheap 503s.

---

## PART 14 — Frontend request behavior

Confirmed polling/loops in `src/`:

- **Interview waiting queue** (`InterviewQueuePage.tsx`): `POLL_MS = 3000` → `GET /api/queue/status/{id}` **every 3 s**, but **only while a student is on the "system busy" waiting screen** (i.e. only when already at capacity). Cleared on unmount/admit.
- **Assessment loading** (`AssessmentLoadingPage.tsx`): `setInterval(checkStatus, 2500)` → `GET /sessions/{id}/assessment/status` **every 2.5 s**, only while an assessment is processing, stops on completion/failure.
- **Interview chat** itself is **event-driven, not polled**: it uses **SSE streaming** (`patientStreamService.ts`) or a single atomic POST — no polling loop for messages.
- **Admin System/Traffic dashboard**: `systemApi.ts` exposes `fetchSystemLive`/`fetchSystemOverview`, but I found **no `setInterval`** wiring in the staged components — refresh appears manual/on-demand (**NOT VERIFIED** for the un-staged dashboard component; regardless it's **admin-only**, so it's not student-scale load).
- **Duplicate requests:** guarded — `inFlightExchangeRef` + idempotent `client_turn_id`; streaming→atomic fallback reuses the same id. No evidence of double-submit on send.
- **Reconnect/stream retry:** the stream falls back to the atomic path once on a pre-sentence failure; no aggressive reconnect loop observed.

**Polling load math (worst case, all students simultaneously in a polling screen):**
- 70 students all *waiting in the interview queue*: 70 / 3 s ≈ **23 req/s** of cheap `queue/status` calls.
- 70 students all *waiting on assessment results*: 70 / 2.5 s = **28 req/s** of cheap status calls.
- These are small, indexed, no-provider calls — negligible for the DB, but they **do consume anyio threads** on the single worker, competing with provider-blocked threads. At 170 that's ~57–68 req/s of polling, still light but no longer free on one worker.

Realistically only a *fraction* of students are on a polling screen at once (most are actively interviewing, which is SSE, not polling), so steady-state polling load is well below these ceilings.

---

## PART 15 — Telemetry / dashboard validity

**The telemetry is unusually honest — this is a real strength.** Every value is either measured or explicitly reported as unavailable; I found **no fabricated metrics**. Sources (`core/telemetry.py`, `services/traffic_service.py`, `services/system_service.py`):

| UI metric | Endpoint → source | Real / estimated? | Scope caveat |
|---|---|---|---|
| Active users | `/admin/system/*` → `LiveRegistry.active_users` (JWT-decoded in middleware) | **Real** (activity-based, 120 s window) | **Per worker** (in-memory) |
| Live interviews | `LiveRegistry` sessions | **Real** | Per worker |
| Requests/min, in-flight, p50/p95/p99 | `telemetry.http` RollingWindow | **Real** (bounded reservoir) | Per worker |
| OpenAI active / rpm / p95 / 429 / retries / tokens | instrumented OpenAI client | **Real** (provider-reported tokens) | **Counters per worker**; concurrency *active* is global if Redis on |
| ElevenLabs active / rpm / 429 | instrumented client | **Real** | Per worker counters |
| Interview / TTS concurrency "active" | `DistributedSemaphore.active_count()` (Redis `ZCARD`) | **Real, fleet-wide** when Redis on (`active_scope:"global"`), else `"process"` or `None` | Honest scope label |
| Assessment queued/processing/oldest | indexed query on `assessment_runs` | **Real, DB-backed (global)** | Shared across workers |
| Workers observed vs configured | `worker_registry.observed_workers` (Redis heartbeats) vs `app_workers` | **Real**; mismatch shown, not hidden | Needs Redis; else "local_only" |
| CPU / memory | `psutil` | **Real**, or "Not available" if psutil missing | Per process/host |
| DB pool | SQLAlchemy `pool.checkedout/size/overflow` | **Real** (Postgres); "N/A — SQLite" in dev | Per worker |
| Redis status | live `PING` in `redis_health()` | **Real, always fresh** | — |

**Could any metric falsely say "healthy"?** The main honest-but-easy-to-misread traps, all documented in `docs/TRAFFIC.md §9–11`:
1. **Per-worker counters look low.** With 4 workers, the dashboard shows **the one worker that served your request** — so "OpenAI rpm" or "active users" can look ~¼ of the true fleet total. Not fake, but easy to under-read. (Concurrency *active*, assessment queue, and DB pool are the exceptions — those are global/DB-backed.)
2. **OpenAI capacity state (BUSY/PROTECTING/CRITICAL) is process-local**, so the adaptive assessment throttle can trip *later* than the fleet's true aggregate usage warrants.
3. **During a *simulated* load test the dashboard's provider numbers reflect *mock* activity** (canned latency), which can look healthier than a real run would (see Part 16). The load-test UI does label provider mode, but a casual reader could conflate "70 users passed (simulated)" with "70 real users OK."

None of these are *dishonest*; they're scope caveats the docs state plainly. The dashboard won't invent a green light, but a reader who ignores the per-worker/simulated caveats can over-trust it.

---

## PART 16 — Load-test validity (why 70 can "pass" while 10 real users struggle)

**What one virtual user actually does** (`load_tests/worker.py`): real HTTP against a real backend — `POST /api/auth/login` (pre-provisioned `is_load_test` account), `GET /api/cases`, `POST /api/sessions`, N × interview turns (bulk `/messages` *or*, in `streaming_voice` mode, the real SSE `/messages/stream` with **one sequential `/voice/synthesize` per sentence**), optional `complete`, optional `assessment` + poll, with realistic think-time. **It does not mock the client side, does not bypass auth, and does hit the real endpoints and write real DB rows.** The `streaming_voice` mode is genuinely realistic of browser behavior.

**But here's the crux — what it usually *doesn't* exercise:**

- **Provider mode defaults to `SIMULATED_AI`.** In simulated mode the controller (`load_test_service.create_job`) calls `runtime_config_service.set_mock_ai(enabled=True)`, a **global DB flag**. Then `openai_client` and `elevenlabs_client` return **canned responses with fixed latency** (`mock_model_latency_ms=800`, `mock_tts_latency_ms=300`) — **no real OpenAI/ElevenLabs call, no real 429s, no real variable latency, no real token/RPM limits.** So a "70 users passed" simulated run proves the **application plumbing** (auth, DB, semaphores, SSE, queue) holds — it does **not** prove OpenAI/ElevenLabs can serve 70 concurrent students at real latency and real rate limits.
- **Real-provider runs are capped at `load_test_real_provider_max_users=10`** and require explicit confirmation. So the *only* runs that exercise real providers are ≤10 users — which is *exactly why 10 real users can reveal problems a 70-user simulated pass hid.*
- **Does it bypass auth/providers?** Auth: **no** (real login). Providers: **yes, in simulated mode** (mock doubles). **Does it simulate the browser?** The `streaming_voice` mode: closely. The default bulk mode: less so (one TTS call per turn instead of per sentence).
- **Does "70 users passed" mean 70 *concurrent*?** In `worker.py` the driver spawns `target_users` threads (optionally ramped), each looping until the deadline, and reports `maxActiveUsers`. So it's **~70 concurrent virtual students** — but the backend under test was (usually) in **mock provider mode**, and (unless you deployed the 4-worker config) running on **one worker** locally against `127.0.0.1:8000` (`load_test_target_base_url`), which is a different machine profile than a t3.micro in prod.

**So, precisely why 70 simulated can pass while 10 real struggle:**
1. Simulated mode replaces the two slow, rate-limited, failure-prone dependencies (OpenAI, ElevenLabs) with fast deterministic stubs — removing the real bottleneck.
2. Real users hit **real** provider latency (seconds, variable), **real** 429/TPM limits, and **real** ElevenLabs concurrency — which the 15-slot TTS and 20-slot interview semaphores then throttle, producing queueing/503/robotic-voice with far fewer people.
3. A local simulated run may also use a beefier box and/or the multi-worker config, not the t3.micro single-worker prod.

**Recommendation (not applied):** treat simulated PASS as "the app architecture holds," and validate real capacity with the small (≤10) real-provider runs plus real production hardware, exactly as `docs/DEPLOYMENT.md` and `load_capacity_analysis.py` already warn ("do not claim 170 until a real run comes back PASS").

---

## PART 17 — Failure modes (traced)

- **A. Redis down (prod).** Concurrency admission fails **closed**: interviews → clean **503 + Retry-After**; TTS → **409 → browser voice**; assessments → workers stop claiming (jobs stay PENDING). Interview waiting-queue reads Redis-unreachable → falls back to treating capacity as unavailable (students see busy). `/api/health` reports `degraded` (Redis required). **No overload, no crash** — a safe degraded mode. Cost: at-capacity is reached instantly on any Redis blip.
- **B. OpenAI 429.** `provider_retry.classify` marks it retryable; backoff honoring `Retry-After`, up to 3 attempts, **inside the held slot** (no concurrency amplification). Counted in telemetry. If it persists, the turn fails cleanly (student keeps question); nothing saved.
- **C. ElevenLabs concurrency limit reached.** Either the local `tts` semaphore (15) returns 409 → browser voice, or ElevenLabs returns 429/5xx → retried then degrades to browser voice. Interview text unaffected.
- **D. One Uvicorn worker crashes.** In-flight requests fail; browser retries idempotently (`client_turn_id`). Held semaphore slots **self-expire (180 s lease)** — no capacity leak. Assessment jobs it ran are **reaped to PENDING** after 10 min. With 1 worker, a crash = brief full outage until systemd/Docker restarts it (**NOT VERIFIED** that a supervisor auto-restarts in prod).
- **E. PostgreSQL connections exhausted.** New checkouts wait `pool_timeout=30s` then raise → 500s on DB-touching endpoints. Most likely if the non-streaming path is used heavily or workers×pool exceeds Postgres `max_connections`. Health check flips `degraded`.
- **F. 70 students submit simultaneously.** See Part 9: 20 get AI now, ~20 wait ≤2 s, rest get 503 or a queue position; TTS beyond 15 concurrent → browser voice. Providers see ≤20/≤15. No collapse; graceful degradation.
- **G. Admin runs a load test while students use the system.** **This is the one genuinely dangerous interaction.** A **SIMULATED** load test sets the **global** `mock_ai` DB flag → **real students simultaneously get canned/mock AI answers and mock audio** for the duration, and their sessions/assessments are computed on fake data. Also the single-run guard only prevents *concurrent load tests*, not load-test-vs-real-traffic. A **REAL**-provider test (≤10 users) instead competes with students for the same 20/15 semaphores and real provider budget. **Recommendation (not applied): never run load tests against the production instance while students are active; run against an isolated stack.**
- **H. OpenAI slow for 30 s.** Threads/slots stay held longer → the 20 interview slots fill → new turns wait then 503/queue; anyio threads park on OpenAI → cheap endpoints slow on the single worker. Recovers when OpenAI recovers. No storm (retries share slots).
- **I. ElevenLabs slow for 30 s.** Per-sentence TTS calls block up to 20 s each, holding TTS slots → 15 fill → others get 409 → browser voice. Interview text unaffected. Recovers on its own.
- **J. Frontend reconnects repeatedly.** Each reconnect is a fresh SSE request consuming a thread + interview slot; idempotency prevents duplicate saves/charges. Worst case is thread/slot churn producing 503s — cheap, self-limiting, no provider amplification.

---

## PART 18 — Bug / risk audit

Confirmed issues are separated from *possible* ones. Nothing here was fixed.

| ID | Severity | File → symbol | Issue | Trigger | User impact | Suggested fix (not applied) |
|---|---|---|---|---|---|---|
| B1 | **HIGH (operational)** | `services/load_test_service.create_job` + `runtime_config_service.set_mock_ai` | Simulated load test flips a **global** `mock_ai` flag affecting **all live users**, and the single-run guard doesn't isolate load-test vs real traffic. | Admin starts a simulated test while students are interviewing. | Real students silently get mock AI + mock voice; their assessments run on fake transcripts. | Refuse to start (or require a big confirmation) when live sessions exist; or scope mock to load-test accounts only. **CONFIRMED.** |
| B2 | **MEDIUM** | `scripts/start.sh` / `Dockerfile` | Ships **single Uvicorn worker** while all the fleet-wide machinery (Redis semaphores, worker registry, multi-worker docs) targets 4. | Default deploy. | The Redis infra is dormant; one worker + 40 threads is the real ceiling; a crash = full outage. | Run the documented `--workers 4` (or gunicorn+uvicorn workers) with Redis, as DEPLOYMENT.md describes. **CONFIRMED.** |
| B3 | **MEDIUM** | `interview_service.send_student_message` | Non-streaming turn holds the request DB session open across the OpenAI call. | Streaming disabled (`OPENAI_PATIENT_STREAMING_ENABLED=false`, which is the **default**). | Under load, DB connections tied up for provider latency → pool pressure earlier. | Adopt the same short-session pattern as the streaming path, or enable streaming in prod. **CONFIRMED.** |
| B4 | **MEDIUM** | Starlette anyio pool (not tuned) | Default **40** worker threads; interview(20)+TTS(15) can park ~35 on provider I/O, starving cheap endpoints on one worker. | High concurrent streaming+voice on a single worker. | Login/cases/queue latency spikes when providers are slow. | Multi-worker, and/or raise the anyio limiter *after* deciding real target concurrency. **CONFIRMED (behavioral).** |
| B5 | **LOW** | `core/config.py` `MAX_CONCURRENT_TTS_REQUESTS=15` vs `.env.example`=10 vs docs=10 | Doc/code mismatch on the TTS cap. | Operator trusts `.env.example`/docs. | Wrong mental model of TTS capacity; mis-tuned for the ElevenLabs plan. | Reconcile config default, `.env.example`, and `docs/TRAFFIC.md`. **CONFIRMED.** |
| B6 | **LOW** | `runtime_config_service.mock_ai_enabled/openai_runtime/elevenlabs_runtime` | Called with `db=None` on the hot path → each opens its **own** DB session/query per turn & per TTS call. | Every chat turn / TTS request. | Extra small pool checkouts + `SELECT system_settings` per turn. | Pass the request session, or cache runtime config with short TTL. **CONFIRMED.** |
| B7 | **LOW** | Broad `except Exception` in telemetry/usage/heartbeat/queue-prune | Errors swallowed to protect the turn. | Any Redis/DB hiccup in those paths. | Silent metric/heartbeat gaps; correct for turn safety, but can mask problems. | Keep behavior; add rate-limited warning logs (several already do). **CONFIRMED (intentional).** |
| B8 | **LOW** | `backend/ptai.db` + `ptai.db.pre-0007.bak` committed in the folder | A dev SQLite DB (and backup) live in the project dir. | — | Confusion / accidental use; not a prod path. | `.gitignore` and remove from the working tree. **CONFIRMED.** |
| B9 | **LOW / NOT VERIFIED** | `main.py` CORS `allow_credentials=True` + `cors_origin_list` | Safe *iff* prod `CORS_ORIGINS` is set to real origins (default is localhost). | Misconfigured prod env. | If left as `*`/localhost, browser auth issues or overly-open CORS. | Confirm prod `CORS_ORIGINS`. **NOT VERIFIED** (env-dependent). |
| B10 | **INFO** | No global circuit breaker | Provider outage ⇒ every turn pays up to 3 backoff attempts before failing. | Sustained OpenAI/EL outage. | Added latency during outages (not a storm). | Optional breaker later, as docs note. **CONFIRMED (accepted).** |

**Security posture (checked, and it's strong):** JWT secret is **fail-closed** in prod (refuses to boot with the dev default / <32 chars); debug forced off in prod; API keys stay server-side (never sent to the browser; never logged — ElevenLabs key only in the `xi-api-key` header); ownership checks on every session/assessment route (never leaks existence); passwords bcrypt-hashed; login brute-force throttled; request-size caps before paid calls; non-root Docker user; provider keys encryptable at rest. I found **no key logging, no SQL injection surface (all ORM/parameterized), no obvious auth bypass.**

---

## PART 19 — Scalability bottleneck ranking (derived from the code)

1. **OpenAI real latency × the interview semaphore (20)** — *the* gate on concurrent live interviews. Appears at **~20+ simultaneous active turns**. Symptoms: waits then **503/queue**. Fleet-wide (Redis).
2. **ElevenLabs real concurrency × the TTS semaphore (15)** — voice degrades first. Appears at **~15+ simultaneous sentence-synths fleet-wide** (≈ far fewer than 15 *students*, since each speaks several sentences). Symptoms: **robotic/browser voice**, delayed audio, 409s.
3. **Single Uvicorn worker + t3.micro (current deploy)** — one process, ~1 GB RAM, 40 threads. Bites at **~30–40 concurrent in-flight requests** or on memory. Symptoms: rising latency across *all* endpoints, possible OOM. (Resolved by the planned 4-worker/larger-instance config.)
4. **Non-streaming DB-session-across-OpenAI (if streaming off)** — DB pool (15/worker) pressure. Appears at **~15+ concurrent non-streaming turns/worker**. Symptoms: pool-timeout 500s. Avoided by streaming.
5. **Redis as a hard dependency in prod** — not a throughput bottleneck (tiny ops) but a **liveness** one: Redis down ⇒ interviews 503. Appears **any time** Redis blips.
6. **OpenAI TPM/RPM tier limits** — `250k TPM / 3k RPM` defaults. At the 20-slot cap you stay well under RPM; TPM depends on prompt/response size. Appears only if the tier is smaller than configured. Symptom: provider 429s.
7. **Anyio 40-thread pool starving cheap endpoints** (companion to #3) — same regime as #3.
8. **Frontend polling (queue 3 s / assessment 2.5 s)** — last; only meaningful at 170 if many sit on polling screens. Symptom: extra cheap request volume on the single worker.

---

## PART 20 — Capacity model

Distinguishing **code-derived** limits (C), **config-derived** (K), **external-provider** (X), and **estimate** (E). Assumes streaming ON and Redis up. Real numbers require the real-provider load runs the docs call for — **these are reasoned expectations, not measurements.**

| Concurrent users | Backend (1 worker / t3.micro) | OpenAI | ElevenLabs | Redis | Database | Expected experience |
|---:|---|---|---|---|---|---|
| 1 | Idle | 1 call | ≤ few sequential | trivial | trivial | Smooth, realistic voice. |
| 5 | Comfortable | ≤5 of 20 (K) | ≤5 of 15 (K) | fine | fine | Smooth. |
| 10 | Fine | ≤10 of 20 | approaching 15 (X/K) if all speaking | fine | fine | Mostly smooth; occasional robotic sentence if bursty. |
| 20 | Threads busy (E) | **at the 20 cap** (K) | **TTS saturating** (K/X) | fine | pool ~ok | First waits (≤2 s); some browser-voice fallback. |
| 30 | ~40-thread pressure (C/E) | queue/503 beyond 20 | frequent browser voice | fine | pool tight if streaming off | Noticeable queueing; degraded voice for some. |
| 70 | **Single worker strained** (C/E) | steady 503/queue past 20 | mostly browser voice under bursts | fine | ok (streaming) / tight (non-stream) | Works *if* queue UX absorbs overflow; voice often robotic; needs multi-worker to feel good. |
| 170 | **Not viable on 1 worker/t3.micro** (E) | 20-cap → long queues unless cap raised + tier checked (K/X) | TTS 15-cap → widespread browser voice unless raised + EL plan supports it (K/X) | fine (tiny ops) | needs 4-worker pool math + Postgres headroom (K) | **Requires** the planned 4-worker + Redis + larger instance, and tuned semaphores validated by real runs. |

**Which are code vs config vs external vs estimate:** the 20/15/3 caps and pools are **config** (tunable live/at boot); the 40-thread pool and short-session patterns are **code**; OpenAI/ElevenLabs true concurrent throughput and 429 behavior are **external** (your paid tier — **NOT VERIFIED**); the "feels smooth up to N" thresholds are **estimates** pending the real-provider load runs.

---

## PART 21 — Teach me the backend (the receptionist analogy, then the real terms)

Picture a clinic:

- **Nginx = the front-desk receptionist.** Everyone enters through one desk; it directs them inside. It doesn't treat patients; it just routes. *(Real term: reverse proxy / TLS terminator.)*
- **The Uvicorn worker = one clinician who can juggle many patients.** Today there's **one** clinician with **40 hands** (threads). While one hand waits on a lab result, the other hands keep working. *(Real term: an ASGI worker process running sync handlers in a thread pool.)*
- **Redis = the traffic controller with a clipboard and a numbered-ticket dispenser.** It hands out a **fixed number of "you may see the specialist now" tickets** (20 for OpenAI, 15 for voice) so the specialists never get mobbed, keeps the **waiting-room line** in order, and takes a **roll call** of which clinicians are on shift (heartbeats). If the traffic controller goes home, the clinic **stops admitting** new specialist visits rather than mobbing the specialists. *(Real terms: distributed semaphore, FIFO queue, worker registry — fail-closed.)*
- **PostgreSQL = the permanent filing cabinet.** Every patient, transcript, and grade is filed here durably. Even if the clinic closes for the night (restart), the files remain — including the "to-be-graded" tray (the assessment queue is literally a drawer in this cabinet). *(Real term: the system of record; the assessment queue is a DB table.)*
- **OpenAI = the outside specialist** who writes what the simulated patient says. The clinician phones them, waits (blocking a hand, not the whole clinic), and files the answer.
- **ElevenLabs = the voice actor** who reads the patient's lines aloud — phoned **separately**, one sentence at a time.
- **The React frontend = the patient's desk/tablet.** It shows the conversation, plays the audio, and occasionally asks "is my grade ready yet?" (polling).

The elegance is in the **tickets** (semaphores) and the rule that **you never keep the filing cabinet drawer open while you're on hold with a specialist** (short DB sessions around provider calls). Those two ideas are what make it scale gracefully instead of collapsing.

---

## PART 22 — One real request, debugger-style (streaming chat turn)

Open these files and follow along:

```
1. src/pages/InterviewPage.tsx → performStreamingTurn()
     builds clientTurnId, calls patientStreamService
2. src/services/patientStreamService.ts
     POST /api/interviews/{sid}/messages/stream  (SSE, fetch reader)
3. backend/app/api/interviews.py → send_message_stream()
     - guards streaming flag (409 if off)
     - authorize_session_from_token()  ← dependencies/auth.py (short DB session, closed)
     - returns StreamingResponse(interview_stream_service.stream_student_message(...))
4. backend/app/services/interview_stream_service.py → stream_student_message()
     - _load_context()  → SHORT DB session #1: SessionRepository.get, TranscriptRepository.list_turns,
                            speaker_router.resolve_for_case  → snapshot, session CLOSED
     - interview_slot().__enter__()  ← core/concurrency.py → DistributedSemaphore("openai_interview").acquire
                            (Redis Lua; raises 503 here if full, before any streaming)
     - returns _stream_events(...)
5. _stream_events()  (the SSE generator)
     - stream_patient_response()  ← patient_engine/streaming_engine.py
         → openai_client.stream_text()  ← patient_engine/openai_client.py
             client.responses.create(model=gpt-4o-mini, stream=True)   [BLOCKING iter, retried only at start]
         → sentence segmentation + validation (sentence_stream.py)
     - yield SSE: event:speech → event:sentence(0..n) → collect StreamCompleted
     - _commit_turn()  → SHORT DB session #2: TranscriptRepository.append_turn (student + patient),
                          SessionRepository.add_disclosed_fact_ids / set_active_topic, db.commit(),
                          usage_recorder.record_openai_usage() → ai_usage_events row, session CLOSED
     - yield SSE: event:final  (turnId, patientText, sessionStatus, speech)
     - finally: interview_slot.__exit__  → semaphore released (even on disconnect/error)
6. Browser, per SSE 'sentence' event → patientVoiceService.ts
     POST /api/voice/synthesize {caseId, text, sessionId}
7. backend/app/api/voice.py → synthesize()
     - _load_synth_context()  → SHORT DB session: auth + approved-text/voice resolve, CLOSED before provider
     - audio_cache.get()  (in-memory)  → hit? stream cached MP3 and return
     - tts_slot().acquire()  ← core/concurrency.py (Redis "tts" semaphore); if full → 409 → browser voice
     - client.stream_speech()  ← voice/elevenlabs_client.py  (httpx stream, first chunk pulled eagerly)
     - usage_recorder.record_elevenlabs_usage()  (new short session, after provider confirmed)
     - StreamingResponse(audio_stream())  → MP3 chunks to browser; slot released in finally
8. Browser plays audio (useVoiceConversation.ts state machine); InterviewPage renders the patient bubble.
```

**No DB session is ever held open across steps 5's OpenAI stream or step 7's ElevenLabs stream** — that's the design's spine.

---

## PART 23 — What to learn, mapped to your files

1. **HTTP request lifecycle** → `backend/app/main.py` (app factory, middleware, routers).
2. **Reverse proxy / Nginx** → `docs/DEPLOYMENT.md` (the Nginx + systemd section).
3. **FastAPI routing & dependencies** → `backend/app/api/interviews.py`, `backend/app/dependencies/auth.py`.
4. **Sync-in-threadpool vs async/await** → `backend/app/core/concurrency.py` (its docstring explains *why* everything is sync) + any `api/*.py` handler.
5. **Uvicorn workers** → `backend/scripts/start.sh` and the `--workers` discussion in `docs/DEPLOYMENT.md`.
6. **SQLAlchemy engine/pool/sessions** → `backend/app/database/connection.py` (esp. `get_db` vs `get_db_factory`).
7. **PostgreSQL / migrations** → `backend/app/database/migrations/versions/` (read 0001, 0011, 0017).
8. **Redis distributed semaphore** → `backend/app/core/distributed_semaphore.py` (the Lua script) + `core/redis_client.py`.
9. **Queues & semaphores (the difference)** → `backend/app/services/interview_queue.py` (real queue) vs `core/concurrency.py` (semaphore) vs `core/assessment_worker.py` (DB-as-queue).
10. **API rate limiting** → `backend/app/core/rate_limit.py`.
11. **OpenAI streaming** → `backend/app/patient_engine/openai_client.py` (`stream_text`) + `patient_engine/streaming_engine.py`.
12. **ElevenLabs streaming** → `backend/app/voice/elevenlabs_client.py` + `backend/app/api/voice.py`.
13. **Retries/backoff** → `backend/app/core/provider_retry.py`.
14. **Observability** → `backend/app/core/telemetry.py` + `backend/app/services/traffic_service.py` + `docs/TRAFFIC.md`.
15. **Load testing** → `backend/load_tests/worker.py` + `backend/app/services/load_test_service.py` + `backend/app/services/load_capacity_analysis.py`.
16. **Scaling story end-to-end** → read `docs/DEPLOYMENT.md` and `docs/TRAFFIC.md` last, with all the above in mind.

Suggested order: 1→3→4 (understand sync-in-threadpool — the single most important idea here), then 6→7 (data), then 8→9→10 (traffic control), then 11→12→13 (providers), then 14→15→16 (observe & scale).

---

## PART 24 — Executive report

**1. Current architecture.** A single-EC2, Nginx→**single-Uvicorn-worker** FastAPI app (sync handlers on a 40-thread pool), PostgreSQL for durable state, and Redis (required in prod) for three coordination jobs only: concurrency semaphores, an interview waiting queue, and worker heartbeats. React SPA frontend using SSE for chat and separate per-sentence HTTP for voice. The codebase is *designed* for 4 workers + Redis at 170 students, but the shipped launch command runs one worker.

**2. Request flow.** Browser → Nginx → Uvicorn worker (threadpool) → JWT auth → short DB reads → Redis semaphore admission → OpenAI (blocking, in-slot, retried-at-start) → SSE sentences to browser → browser calls ElevenLabs endpoint per sentence (separate, TTS-semaphore-gated) → short DB commit of exactly one student + one patient turn → usage row. No DB connection is held across any provider call in the streaming/voice paths.

**3. Database architecture.** SQLAlchemy 2.0 sync, per-worker pool (5+10), Alembic-migrated Postgres. Clean schema with the right uniqueness guards (idempotent turns; one active assessment per session). Assessments run off-request via a **DB-table queue** drained by background threads. SQLite is dev/test only.

**4. Redis architecture.** Fleet-wide counting semaphores (Lua/ZSET, lease-expiring), a FIFO interview admission queue, and self-expiring worker heartbeats. **Fail-closed** in prod: Redis down ⇒ interviews 503, voice→browser, assessments pause. Not used for caching/sessions/rate-limits.

**5. Worker architecture.** One process = one full app copy with its own pool/telemetry/rate-limits. Today: **one** worker (crash = outage until restart). Planned: 4 workers via Uvicorn multiprocess behind the same Nginx address; the kernel load-balances the shared socket. Semaphores are global (Redis) so 4 workers still enforce the *same* 20/15/3 caps.

**6. OpenAI architecture.** `gpt-4o-mini` Responses API, streaming, structured JSON for turns; bounded history (12 turns/12k chars); provider-reported token accounting recorded once; transient-only retries in-slot; assessments hard-capped at 3 logical calls. No key rotation.

**7. ElevenLabs architecture.** Per-sentence, **sequential-per-student** TTS over a shared keep-alive httpx client; TTS semaphore 15; in-memory audio cache; degrades to browser voice on any saturation/failure. Key stays server-side.

**8. Top 10 bugs/risks (ranked).** B1 global-mock-during-live-load-test (HIGH); B2 single-worker-vs-designed-4 (MED); B3 non-streaming holds DB across OpenAI (MED); B4 40-thread pool starvation (MED); B5 TTS cap doc/code mismatch 10-vs-15 (LOW); B6 per-turn extra runtime-config DB sessions (LOW); B9 CORS prod-config unverified (LOW/NV); B10 no circuit breaker (INFO); B7 broad excepts masking signals (LOW/intentional); B8 dev SQLite DB committed (LOW).

**9. Top 10 scalability problems (ranked).** (1) OpenAI latency×20-semaphore; (2) ElevenLabs×15-semaphore→robotic voice; (3) single worker + t3.micro; (4) non-streaming DB-session-across-OpenAI; (5) Redis as prod liveness dependency; (6) OpenAI TPM/RPM tier (unverified); (7) 40-thread pool; (8) polling load at 170; (9) per-worker telemetry under-reads true fleet usage; (10) load-test simulated mode hides real provider limits.

**10. Why real users fail before load tests.** Because the standard load test runs in **SIMULATED_AI** mode, which globally replaces OpenAI and ElevenLabs with fast, deterministic, rate-limit-free stubs. That removes the two real bottlenecks, so 70 simulated users sail through while **10 real users** hit real seconds-long provider latency, real 429/TPM limits, and the real 15-slot TTS/20-slot interview caps — plus the real single-worker t3.micro. The test validates the *plumbing*, not the *providers*.

**11. Current bottleneck.** On the **current single-worker/t3.micro** deploy: the combination of **one Uvicorn worker + real provider latency holding threads/semaphore slots**. The semaphores keep it *safe* (503/queue/browser-voice) rather than crashing, so the felt limit is "students queue or get robotic voice past ~20 concurrent interviews / ~15 concurrent voice synths," well before any provider hard-limit.

**12. 70-user readiness: PARTIALLY READY.** The *architecture* can support it; the *current deployment* (1 worker, t3.micro, streaming default OFF) cannot comfortably. Ready **only** after: enable streaming in prod, run the documented 4 workers + Redis on a larger instance, and validate with a real-provider run — until then it's "designed for," not "proven for."

**13. 170-user readiness: NOT READY (as deployed); designed-for with the planned config.** Requires the 4-worker + Redis + larger-instance setup, tuned/validated 20-and-15 semaphores against your **actual** OpenAI and ElevenLabs tiers (both **NOT VERIFIED**), Postgres `max_connections` re-checked against 4×15, and a real-provider staged run returning PASS. The code is ready for this shape; the numbers must be earned by measurement.

**14. What to fix first.**
- **P0 (now):** Stop running load tests against the live instance (B1); deploy the documented **4 workers + Redis** (B2) or at minimum confirm a supervisor auto-restarts the single worker; **enable streaming in prod** so B3/B4 don't bite.
- **P1 (before 70):** Reconcile the TTS cap (B5) and **verify your real OpenAI TPM/RPM and ElevenLabs concurrency tiers**, then tune `MAX_CONCURRENT_AI_INTERVIEWS`/`MAX_CONCURRENT_TTS_REQUESTS` and run a **real-provider** staged load test; confirm prod `CORS_ORIGINS` (B9); size Postgres for 4×15.
- **P2 (before 170):** Move rate-limit + OpenAI RPM/TPM telemetry to Redis for true fleet-wide accuracy; consider raising the anyio pool deliberately; add cleanup for load-test rows; larger instance.
- **P3 (optimization):** Cache runtime config to remove per-turn extra DB sessions (B6); optional circuit breaker (B10); `.gitignore` the dev SQLite DB (B8); rate-limited warning logs on the swallow-and-continue paths (B7).

**15. Learning roadmap.** Follow Part 23 in the order given, starting with the sync-in-threadpool model (`core/concurrency.py` docstring) because it's the key that makes everything else make sense, then data (SQLAlchemy/Postgres), then traffic control (Redis semaphore + queue), then providers (OpenAI/ElevenLabs streaming), then observability + load testing + the two `docs/` files last.

---

### Appendix — confirmed vs possible vs not-verified

- **CONFIRMED from code:** single-worker launch; all-sync handlers (no event-loop blocking); short-session pattern in streaming/voice; Redis fail-closed concurrency; interview queue = Redis ZSET; assessment queue = DB table; idempotency guards; bounded OpenAI history; once-only provider-reported token accounting; assessment 3-call cap; global mock_ai flag during simulated load tests; TTS cap doc/code mismatch (15 vs 10); honest telemetry.
- **POSSIBLE (behavioral, load-dependent):** 40-thread pool starvation; non-streaming DB-pool pressure; polling load at 170. These follow logically from the code but need a real run to quantify.
- **NOT VERIFIED (outside the repo):** the production Nginx config; whether prod runs 1 or 4 workers right now; a process supervisor/auto-restart; actual `CORS_ORIGINS` in prod; your real OpenAI TPM/RPM tier and ElevenLabs concurrency plan; any client-side auto-retry aggressiveness; the admin dashboard's polling interval.
