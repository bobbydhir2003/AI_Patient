# Production Deployment (4 Uvicorn Workers + Redis)

This documents the recommended production configuration for the planned
scale-up (target: 170 concurrent students, being validated - see
`PRIORITY_J_LOAD_CAPACITY_TESTING_REPORT.md` and `docs/TRAFFIC.md` for load-test
results). **No AWS resources are created or changed by the application** -
this is a deployment guide, not automation. Nothing here is applied
automatically; provision Redis and change the systemd unit yourself.

## Current vs planned

| | Current | Planned |
|---|---|---|
| EC2 | t3.micro, 2 vCPU, ~1 GB RAM | Larger instance, ~4 GB RAM as a starting point |
| Uvicorn workers | 1 | 4 |
| Redis | none | required (global concurrency control) |
| PostgreSQL | same instance/RDS-less, as today | unchanged - no migration |
| Nginx | unchanged | unchanged |

## Why Redis is now required in production

With more than one Uvicorn worker process, the OpenAI/TTS/assessment
concurrency guards must be **shared**, not per-process - otherwise 4 workers
each enforcing "20 concurrent OpenAI calls" adds up to 80 fleet-wide, silently
multiplying your real provider limit. `core/distributed_semaphore.py` backs
these guards with Redis so the configured limit is the true fleet-wide cap.

`Settings.redis_required` defaults to `true` in `production`/`staging`: the
app **refuses to start** if `REDIS_URL` is unset in those environments (fails
closed, same pattern as the existing `JWT_SECRET_KEY` check). This is
intentional - do not deploy this change to a production host without
provisioning Redis first, or explicitly set
`REDIS_REQUIRED_FOR_CONCURRENCY=false` as a temporary, less-safe fallback for
a single-worker transition period only.

## Redis provisioning

Any small managed or self-hosted Redis works - this app only uses simple
ZSET/EVAL commands for a handful of small keys (`ptai:sem:*`); it is not used
for caching or sessions, so throughput/memory requirements are minimal
(comfortably fits the smallest available tier, e.g. AWS ElastiCache
`cache.t3.micro`, or a small Redis container on the same host/VPC for a first
cut). Requirements:
- Reachable from the EC2 instance (same VPC/security group).
- `REDIS_URL` set, e.g. `redis://<host>:6379/0` (add auth/TLS per your Redis
  provider's standard connection string format - the app just passes the URL
  to `redis.Redis.from_url(...)`).

## Recommended systemd unit

No systemd unit currently exists in this repo (only `backend/Dockerfile`,
which already runs `uvicorn app.main:app`). If deploying via systemd rather
than the Docker image, use something like:

```ini
# /etc/systemd/system/ptai-backend.service
[Unit]
Description=PT AI Patient Simulator API
After=network.target

[Service]
Type=simple
User=ptai
WorkingDirectory=/srv/ptai/backend
EnvironmentFile=/srv/ptai/backend/.env
ExecStart=/srv/ptai/backend/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 \
    --workers 4 \
    --timeout-keep-alive 30
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

Equivalent direct command:

```bash
cd /srv/ptai/backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

Notes:
- `--workers 4` uses Uvicorn's built-in multiprocess mode (no Gunicorn needed).
  Each worker still serves requests with normal async I/O; 4 workers is a
  process-count decision, not a change to how any single request is handled.
- Nginx continues to reverse-proxy to `127.0.0.1:8000` exactly as today - no
  Nginx config change is required for the worker-count change itself.
- `LimitNOFILE=65536` gives headroom for the additional DB/Redis/HTTP
  connections 4 workers open concurrently at 170-student scale.

## Required/updated environment variables for this change

```bash
# Redis - REQUIRED in production
REDIS_URL=redis://<redis-host>:6379/0

# OpenAI capacity - match your real project tier
OPENAI_TPM_LIMIT=250000     # adjust to your actual tier
OPENAI_RPM_LIMIT=3000       # adjust to your actual tier

# Concurrency (fleet-wide once Redis is connected)
MAX_CONCURRENT_AI_INTERVIEWS=20   # start here; tune from load-test results
MAX_CONCURRENT_TTS_REQUESTS=10    # start here for eleven_flash_v2_5; tune from load-test results

# Deployment description (informational, shown on the traffic dashboard)
DEPLOYMENT_MODE=multi_worker
APP_WORKERS=4

# Database pool (per worker; 4 workers x (5+10) = 60 max connections - keep
# under your Postgres max_connections with headroom for admin/migration use)
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10

# Load testing - allow staged runs up to the scalability target
LOAD_TEST_MAX_USERS=170
```

Everything above has a safe default already in `backend/.env.example` and
`core/config.py`; only `REDIS_URL` and the OpenAI TPM/RPM values (to match
your real provider tier) must be set explicitly for production.

## PostgreSQL sizing check

Confirm Postgres `max_connections` comfortably exceeds
`APP_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW)` plus headroom for
migrations/admin access - e.g. with the defaults above, 4 x 15 = 60; the
default Postgres `max_connections` (100) has room to spare. If you raise
`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`, re-check this math.

## Verifying the deployment

1. `GET /api/health` should report `"status": "ok"`, `"redis": "connected"`.
2. Admin → System Dashboard should show Redis `connected` and OpenAI/TTS
   concurrency scope as fleet-wide (`concurrency_scope: "global (redis)"` on
   `GET /api/admin/system/traffic/capacity`).
3. Run a staged load test (see below) before claiming any concurrency target
   is supported in practice.

## Staged load testing (20 / 70 / 100 / 130 / 170 users)

The existing load-test tooling (`backend/load_tests/loadtest.py`, or the
in-app job runner under Admin → Load & Capacity Testing) already drives a
realistic student flow: register/login, case selection, session creation,
multiple interview turns with think-time, completion, and assessment
submission/poll. `LOAD_TEST_MAX_USERS` now allows targeting up to 170.

```bash
# Simulated-AI mode first (zero provider spend) against a real deployment:
python -m load_tests.loadtest --base-url https://<host> --concurrency 20  --sessions 20  --turns 6
python -m load_tests.loadtest --base-url https://<host> --concurrency 70  --sessions 70  --turns 6
python -m load_tests.loadtest --base-url https://<host> --concurrency 100 --sessions 100 --turns 6
python -m load_tests.loadtest --base-url https://<host> --concurrency 130 --sessions 130 --turns 6
python -m load_tests.loadtest --base-url https://<host> --concurrency 170 --sessions 170 --turns 6
```

Or via the admin dashboard: create a `ramp` job with `targetUsers` at each
stage, then a separate `spike`/`stress` job to see behavior when many users
arrive at nearly the same time. See `app/services/load_capacity_analysis.py`
for how PASS/PASS_WITH_WARNING/FAIL/INCONCLUSIVE is derived from measured
data only - **do not state "170 concurrent students supported" until a real
run at that level comes back PASS or PASS_WITH_WARNING**; until then, describe
the architecture as "designed for / being validated for 170 concurrent
students."

## LiveKit POC agent worker (Phase 2, optional, behind LIVEKIT_POC_ENABLED)

Not part of the production interview path - only relevant if the LiveKit
voice POC (`app/livekit_agent/`, `app/api/livekit.py`) is enabled. This is a
**second, independent systemd service** alongside `ptai-backend.service`
above - never started inside Uvicorn, never per-interview. It registers ONCE
with LiveKit and receives one job per student interview automatically (see
`app/livekit_agent/worker.py`'s module docstring for the full design) - there
is no `--room`/`--session-id`/`--case-id` to pass; those now arrive as job
metadata from LiveKit's own dispatch mechanism.

```ini
# /etc/systemd/system/ptai-livekit-agent.service
[Unit]
Description=PT AI Patient Simulator - LiveKit Agent Worker
After=network.target ptai-backend.service

[Service]
Type=simple
User=ptai
WorkingDirectory=/srv/ptai/backend
EnvironmentFile=/srv/ptai/backend/.env
ExecStart=/srv/ptai/backend/.venv/bin/python -m app.livekit_agent.worker start
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

Notes:
- **Same `.env` file** as `ptai-backend.service` - the worker needs
  `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`/`LIVEKIT_POC_ENABLED`
  (LiveKit), `REDIS_URL` (so `interview_slot()`/`tts_slot()` share the SAME
  fleet-wide semaphores the FastAPI workers use - this is not optional; a
  worker without Redis configured falls back to a per-process limit, which
  is unsafe the moment more than one worker process exists), `DATABASE_URL`
  (same Postgres, so transcripts land in the same place production writes
  to), `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`.
- `start` is the LiveKit Agents framework's own CLI subcommand (from
  `livekit-agents`'s `cli.run_app`) - not a custom flag of ours. It is
  required; running the module with no subcommand does nothing.
- The worker fails closed at startup (`SystemExit`, non-zero exit code) if
  `LIVEKIT_POC_ENABLED` is not `true` or any of the three LiveKit credentials
  are missing - `Restart=on-failure` will keep retrying every 5s, which is
  intentionally harmless (it just keeps failing closed) until an operator
  fixes the `.env`, not a crash loop that does anything unsafe.
- `Restart=on-failure` also covers a whole-process crash: on restart the
  worker simply re-registers with LiveKit and resumes accepting jobs. A job
  that was mid-flight when the process died is not silently resumed - the
  student sees the same "no response from the agent" thinking-timeout error
  the frontend already handles explicitly (see `livekitPocEngine.ts`), and
  can speak again once the worker is back.
- Per-job crash isolation is handled by the Agents framework itself
  (`JobExecutorType.PROCESS`, the default `WorkerOptions` kept unchanged in
  this codebase) - one interview's job process crashing does not affect any
  other concurrent interview's job or the worker process itself.
- Scaling: one worker process can serve many concurrent interviews (each
  accepted job runs in the framework's own isolated subprocess pool). If load
  testing shows one process is not enough, run a second
  `ptai-livekit-agent.service`-style unit (or bump its own internal
  concurrency settings) - this is additive, matching how `APP_WORKERS` is
  scaled for the FastAPI backend above. Do not assume a specific number
  without measuring it. See "LiveKit horizontal scaling" below for the full
  analysis and the recommended topology for 60-65 concurrent students.

### LiveKit horizontal scaling

**The dispatch mechanism already supports N identical workers with zero code
changes.** `livekit_token_service.py` mints every token with an explicit
`RoomAgentDispatch(agent_name=settings.livekit_agent_name)` entry (see
`test_token_embeds_explicit_agent_dispatch_with_fixed_agent_name`) -
LiveKit Cloud's own dispatcher assigns each new room's job to *any* currently
registered worker process sharing that `agent_name`, load-balanced by each
worker's self-reported load (`WorkerOptions.load_threshold`, prod default
`0.7` - a worker stops receiving new jobs once its own CPU load crosses that
threshold, and the dispatcher routes to a less-loaded one instead). Running
two, five, or twenty copies of `ptai-livekit-agent.service` - on one box or
spread across several - is therefore the intended scaling mechanism, not a
workaround.

**Per-session state is confirmed process-local.** Every `PocAgentSession` is
constructed fresh per job (`entrypoint()`) with its own `session_id`,
`case_id`, `job_id`, `room_id`, turn lock, and (Phase C) in-flight/completed
`clientTurnId` dedup sets - there is no module-level dict/cache anywhere in
`worker.py` keyed by session. Verified explicitly by
`test_two_jobs_do_not_share_state` and Phase C's
`test_two_sessions_have_independent_dedup_and_identity_state`/
`test_two_sessions_deliver_status_to_their_own_room_only`. Additionally, each
accepted job already runs in its own OS process
(`JobExecutorType.PROCESS`, the framework default, unchanged here) - so even
a bug in this isolation would still be contained to one interview, never
leak across a whole worker, let alone across machines.

**Shared capacity control already goes through Redis, not per-process
memory.** `interview_slot()`/`tts_slot()` (`app/core/concurrency.py`) are
Redis-backed distributed semaphores - the SAME ones every FastAPI worker
uses for `/api/interviews` and `/api/voice` - and `patient_adapter.py`
routes every OpenAI/ElevenLabs call through them (see
`test_poc_agent_session_turn_uses_interview_slot`/`_uses_tts_slot`). This
means the *provider-level* concurrency cap (how many simultaneous OpenAI/
ElevenLabs calls are allowed fleet-wide) is already correct no matter how
many LiveKit worker processes or machines are running - it is enforced in
Redis, not in any one process's memory.

**Remaining machine-local requirements to verify before adding a second
machine** (none require code changes, all are `.env`/infrastructure
configuration):
1. `REDIS_URL` must point at ONE shared Redis instance (e.g. ElastiCache)
   reachable from every worker machine - not a `localhost` Redis per box.
   `settings.redis_required` already fails the process closed if
   `REDIS_URL` is unset (see `app/core/config.py`), but nothing stops two
   *different* Redis instances from each being individually "configured" and
   silently maintaining two disconnected sets of counters - this is an
   operational check, not something the code can enforce.
2. `DATABASE_URL` must point at ONE shared, centrally-reachable database
   (the same Postgres production already uses) - `config.py`'s own default
   (`postgresql+psycopg2://ptai:ptai@localhost:5432/ptai`) is a `localhost`
   URL, so an operator who forgets to override it on a second machine would
   silently write transcripts to a second, disconnected database instead of
   failing loudly. Verify `DATABASE_URL` is explicitly set to the shared
   instance in every worker machine's `.env`.
3. `LIVEKIT_AGENT_NAME` (-> `settings.livekit_agent_name`) must be IDENTICAL
   across every worker machine - a typo here would silently create a worker
   that registers but never receives any jobs (LiveKit has no one else to
   dispatch to under that name), which looks like "the worker is running"
   but behaves like "the worker doesn't exist."
4. The agent's fixed participant identity (`AGENT_PARTICIPANT_IDENTITY =
   "patient-agent"`) is reused by every worker process/machine - this is
   intentional and safe, NOT a collision risk: identities only need to be
   unique *within a room*, and every interview has its own room with at
   most one agent participant in it.
5. `WorkerOptions`' capacity-tuning fields (`load_threshold`,
   `num_idle_processes`, `job_memory_warn_mb`, `job_memory_limit_mb`) are
   currently left at framework defaults in `_build_worker_options()` -
   reasonable to start, but should be set explicitly (not left to
   framework-version defaults that could change) once real per-interview
   CPU/memory numbers exist from a load test, so each machine's own capacity
   is declared rather than assumed.

**Recommended topology for 60-65 concurrent students** (to be confirmed by
an actual load test, not assumed - see the load-testing section above):
- **FastAPI backend**: unchanged from the existing `APP_WORKERS`-scaled
  Uvicorn deployment - LiveKit voice traffic barely touches it (one token
  mint per interview start; the rest is WebRTC data/media, not HTTP).
- **LiveKit SFU**: LiveKit Cloud (managed) - no self-hosting concern for
  media routing/relay at this scale.
- **LiveKit agent workers**: 2-3 EC2 instances (start with 2, add a 3rd if
  load testing shows headroom is thin), each running ONE
  `ptai-livekit-agent.service`, all sharing the SAME `LIVEKIT_AGENT_NAME`,
  `REDIS_URL`, and `DATABASE_URL`. Splitting ~65 concurrent interviews
  across 2-3 machines (roughly 22-33 per machine) keeps each machine's
  per-job-process overhead (a full Python interpreter + SQLAlchemy +
  livekit.rtc import graph per accepted job - see the module docstring's
  `JobExecutorType.PROCESS` note) comfortably below the point where CPU/
  memory contention would push `load_threshold` past its `0.7` cutoff and
  start silently rejecting new jobs on that machine mid-event.
- **Redis**: ONE managed instance (e.g. ElastiCache), shared by the FastAPI
  backend AND every LiveKit worker machine - already the case for
  `interview_slot()`/`tts_slot()`; no new capacity concern, since the
  semaphore keys are tiny and the LiveKit agents just add more clients
  against the same limits, not a new kind of load.
- **Database**: the existing shared Postgres instance - no schema or
  connection-pooling change from LiveKit's addition (`patient_adapter.py`
  reuses the same repositories/models every HTTP request already writes
  through).

**Blockers before a real 65-user load test** (see task list): (1) run it
against 2+ worker machines from the start, not one, so the topology above is
validated rather than assumed; (2) explicitly set `WorkerOptions`' capacity
fields per machine once real per-interview CPU/RAM numbers exist; (3)
confirm `REDIS_URL`/`DATABASE_URL` are identically the shared instances on
every machine (item 1/2 above) - a misconfigured second machine would fail
silently in a way that looks like reduced capacity, not an error.
