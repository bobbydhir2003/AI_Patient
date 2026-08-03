# Priority J — Load & Capacity Testing — Final Report

**Scope:** Add a super-admin-only "Load & Capacity Testing" page and backend that
drives realistic virtual-student traffic from a **separate process** and reports
**only real, measured** capacity. No AWS resources created, nothing deployed.

**Result:** Complete and verified locally. 310 backend tests pass (21 new for J);
frontend typecheck clean; a real 10-/4-user simulated run was executed end to end
against a live server and produced genuine measurements.

---

## 1. The absolute rule: no fabricated data

Every value shown is a real runtime measurement or an explicit "Not available".
There are **no** hardcoded metrics, demo graphs, random numbers, invented "Pass",
fake capacity recommendations, or manufactured provider/CPU/memory figures.

- Live metric cards, charts, and percentiles come from the worker's measured
  per-request outcomes (`load_tests/worker.py` → `Metrics`).
- Provider Activity and Infrastructure Health are pulled from the backend's own
  existing real telemetry (`traffic_service.provider_block`, `server_health`,
  `db_pool_stats`, `assessment_block`).
- When a value cannot be measured, the API returns `null` and the UI renders
  "Not available" / "Not configured" (e.g. DB pool on SQLite reports
  `applicable:false`, CPU shows "Not available" when psutil is off).
- Capacity that cannot be established returns `INCONCLUSIVE` with a reason — it
  never invents a number to fill the UI.

## 2. Load generator runs in a SEPARATE process

The generator is `python -m load_tests.worker`, launched via `subprocess.Popen`
by `load_test_service._launch_worker`. It never runs inside FastAPI's request or
event loop. A per-job monitor thread waits on the child and finalizes on exit.
Verified live: the created job reported `workerIdentifier: "pid:27"`.

## 3. Realistic virtual-student workflow

Each virtual student: authenticate (pre-provisioned account) → `GET /api/cases`
→ `POST /api/sessions` → N × `POST /api/interviews/{id}/messages` with jittered
think-time → optional `POST /api/voice/synthesize` (TTS modes) → optional
`complete` → optional `assessment` (+ poll). Ramped start across `rampSeconds`,
held for `durationSeconds`.

## 4. Isolated test identities (never real students)

Virtual students log in only as dedicated `is_load_test = true` accounts
(`loadtest+NNN@loadtest.invalid`), provisioned/reused by
`load_test_service.provision_test_users`. They are ACTIVE students but flagged so
their sessions/turns are load-test artifacts, never academic records. Real
student accounts are never touched. (`test_provision_creates_isolated_users`,
`test_provision_reuses_existing_users`.)

## 5. Two provider modes

- **SIMULATED_AI** — test doubles, zero provider spend. Implemented via a runtime
  DB override for `mock_ai` (`runtime_config_service.set_mock_ai`/`clear_mock_ai`),
  so the same running backend uses mocks for the run, then the override is cleared
  on completion. Supports any user count up to the safety cap (10/20/40/70/100/custom).
- **REAL_OPENAI** and **REAL_OPENAI_TTS** — genuine paid traffic.

## 6. Explicit cost confirmation before paid traffic

Real modes require `confirmRealProvider: true`; without it the API returns HTTP
422 `load_test_confirmation_required` and nothing runs. The UI shows a cost
warning and a blocking `confirm()` before sending. (`test_real_provider_requires_confirmation`,
`test_real_provider_with_confirmation_starts`.)

## 7. Real job model + persistence

`LoadTestJob` (migration `0014`) stores id, created_by, environment, test_type,
provider_mode, target_users, ramp_seconds, duration_seconds, status,
started_at/ended_at, worker_identifier, error_message, and a `results` JSON
(computed summary + capacity analysis). States: PENDING / STARTING / RUNNING /
STOPPING / COMPLETED / FAILED / CANCELLED. Completed-run metadata is persisted
(Postgres in prod); only bounded summary + a down-sampled series are stored — no
massive raw time-series. (`test_finalize_persists_results`.)

## 8. API — all super-admin only

`/api/admin/system/load-tests` — `config`, `POST` create, `recent`, `active`,
`{id}`, `{id}/metrics`, `{id}/stop`. Router-level `require_super_admin`. Regular
admin → 403, student → 403, unauthenticated → 401. (`test_config_rbac`,
`test_create_forbidden_for_admin_and_student`, `test_recent_and_metrics_rbac`;
verified live: no-token config = 401.)

## 9. Single heavy test at a time

`create_job` holds a lock and rejects a second start while any job is
non-terminal (or a worker is registered), returning HTTP 409
`load_test_already_running`. (`test_single_run_conflict`.)

## 10. Safety caps

`target_users` is capped by `load_test_max_users` (100) and `duration_seconds`
by `load_test_max_duration_seconds` (3600s). Over-cap requests → 422.
(`test_target_users_over_cap_rejected`, `test_duration_over_cap_rejected`.)

## 11. Test types

Smoke, Concurrent Student Simulation, Ramp, Spike, Stress (bounded by the caps),
Soak (capability only — the UI exposes it but nothing auto-runs a 60-min test),
AI Traffic, TTS Traffic. Unknown types → 422 (`test_unknown_test_type_rejected`).

## 12. Quick profiles

Quick 10 (2 min), Classroom 20 (5 min), Full Class 70, Stress (bounded), Soak
(long). Profiles only pre-fill the form; nothing runs until the operator clicks
Start.

## 13. Live metrics via real telemetry (polling)

The page polls `{id}/metrics` every 3s while a test runs. The worker writes
atomic JSON snapshots (bounded, down-sampled ≤300 points); the controller reads
them and merges live provider/infra telemetry. No JS-simulated metrics.

## 14. Metric cards

Virtual Users (active/target), Requests/sec, Success rate, Failed, p50/p95/p99,
Peak concurrency — all from measured windows. Verified live: 60–61 requests,
100% success, p50 46.8 ms, p95 ~193 ms, peak concurrency 4.

## 15. Charts from real samples

User-load / throughput / latency lines render the measured `series`; before data
exists they show "Collecting data…" rather than a fake curve.

## 16. Provider Activity + Infrastructure Health

From existing real telemetry: OpenAI/ElevenLabs req-per-min and success rate;
server CPU/memory/uptime (or "Not available"); DB pool ("Not applicable" on
SQLite); assessment queue; HTTP in-flight. Verified live: server.available=true,
cpu 4.4%, uptime 7s, dbPool `applicable:false` (SQLite).

## 17. Transparent capacity analysis

`load_capacity_analysis.analyze` returns PASS / PASS_WITH_WARNING / FAIL /
INCONCLUSIVE with every threshold surfaced under `criteria`. Signals used are all
measured: overall success rate, 5xx/503/429 counts, network errors, p95/p99.

## 18. INCONCLUSIVE when the run is insufficient

If requests < 20, duration < 20s, peak concurrency < 50% of target, or < 3
measured windows, the verdict is INCONCLUSIVE with a reason and a null safe
capacity. **Verified live**: a 5-second run returned INCONCLUSIVE — "Run duration
5s is below the 20s minimum" — instead of inventing a PASS.

## 19. Safe capacity computed from the observed stable region

`recommendedSafeCapacity` is the highest concurrency whose measured window stayed
healthy (≥99% success, p95 ≤ 3000 ms), with an explicit basis/reason. It is never
a preset like "55–65". Unit tests prove it tracks the data: a run healthy to 20
users → 20; a run degrading above 30 → 30. (`test_analysis_pass_and_safe_capacity_from_observed_region`,
`test_safe_capacity_tracks_the_data_not_a_constant`.)

## 20. Observed bottleneck from real signals

Chosen from measured data in priority order: server overload (5xx/503) → rate
limiting (429) → connection failures → latency growth → none observed within the
tested range. Never hardcoded to "OpenAI concurrency".

## 21. Recent Test Runs + empty states

`recent` returns real history; the table shows "No load tests have been run yet."
when empty and the metric area shows empty states before the first run.
(`test_recent_empty_state`, `test_active_none_initially`; verified live: recent
listed the COMPLETED run after finishing.)

## 22. Security preserved; no AWS; not deployed

Priority A/B security is intact — full suite (incl. `test_security_priority_a`,
`test_traffic_priority_b`) passes. `config` reports `awsReady: false` and
`environment: local`; no ECS/Fargate/any AWS resource is created and nothing is
deployed. The AWS path is architecture-ready only (swap the worker launch for a
remote task runner later).

---

## Files

**Backend**
- `app/models/load_test_job.py` — new `LoadTestJob` model + status/provider constants
- `app/models/user.py` — `is_load_test` isolation flag
- `app/database/migrations/versions/0014_load_test_jobs.py` — table + flag (Postgres-verified)
- `app/services/load_test_service.py` — controller: create/get/stop/recent/metrics, single-run guard, provisioning, subprocess launch + monitor, telemetry merge
- `app/services/load_capacity_analysis.py` — transparent PASS/WARN/FAIL/INCONCLUSIVE + observed safe capacity
- `app/api/admin_load_tests.py` — super-admin API
- `app/schemas/load_test_schema.py` — request/response schemas + allowlists
- `load_tests/worker.py` — separate-process metrics-emitting generator
- `app/core/config.py`, `app/services/runtime_config_service.py` — load-test caps + `mock_ai` runtime override
- `app/patient_engine/openai_client.py`, `app/voice/elevenlabs_client.py` — read the effective mock flag
- `app/core/exceptions.py` — load-test errors; `app/main.py` — router registration
- `tests/test_priority_j.py` — 21 tests
- `scripts/live_curl_check.sh` — lightweight real local end-to-end validator

**Frontend**
- `src/pages/admin/system/LoadCapacityTestingPage.tsx` — the page
- `src/services/loadTestApi.ts` — API client
- `src/components/admin/AdminSidebar.tsx` — super-admin-only nav item
- `src/App.tsx`, `src/portal/ProtectedRoute.tsx` — super-admin-gated route

## Verification

- Backend: **310 passed** (batched due to sandbox memory limits), including 21 new J tests.
- Frontend: `tsc --noEmit` clean.
- Real local run: live server + separate worker (pid:27), 60–61 real requests,
  100% success, real latency percentiles, real telemetry merged, INCONCLUSIVE
  verdict for the deliberately short window, run persisted to Recent.

## Not done (per instructions)

No AWS resources, no deployment, no changes to existing AWS infrastructure.
