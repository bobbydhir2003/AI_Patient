# Production Deployment Architecture

This document separates **CURRENT, verified** production architecture (confirmed by direct, read-only inspection of the live EC2 host) from **FUTURE, proposed** architecture (design only — none of it is implemented). No secret values appear anywhere in this document.

Verified against: production host `PT-AI-Patient-Server` (AWS EC2, `t3.xlarge`, `us-east-2`). Exact instance ID/IP are intentionally omitted from this public repository; see internal ops records.

---

## CURRENT architecture (verified)

```
Internet
   |
   v
nginx (TLS via Certbot, server_name is a sslip.io hostname bound to the host's public IP)
   |
   +--> /            static frontend, served from /var/www/ptai
   |                 (SPA fallback: try_files $uri $uri/ /index.html)
   |
   +--> /api/        proxy_pass http://127.0.0.1:8000/api/
   |                 proxy_buffering off; ~300s read/send timeouts
   |                 (needed for SSE-streamed interview responses)
   |
   +--> /docs, /openapi.json   also proxied to 127.0.0.1:8000 (see Security Observations)
                        |
                        v
                  ptai.service (systemd)
                  3 Uvicorn workers, bound to 127.0.0.1:8000
                  Restart=always
                        |
                +-------+-------+
                |               |
           PostgreSQL         Redis
           localhost:5432     127.0.0.1:6379
           (native, apt-      (localhost-only bind,
            installed 18.6)    no requirepass)


Student Browser
      |
      | WebRTC (does NOT go through nginx or ptai.service)
      v
LiveKit Cloud (wss://ai-pt-sddffs5c.livekit.cloud, region "US Central A")
      |
      | job dispatch, under agent_name "ptai-patient-agent"
      v
ptai-livekit-agent.service (systemd)
1 main worker process + ~5 pre-warmed job-executor subprocesses
(the executor subprocesses are the livekit-agents framework's own
 JobExecutorType.PROCESS pool - NOT separate LiveKit workers)
      |
      +--> OpenAI          (patient response generation)
      +--> ElevenLabs      (patient audio synthesis)
      +--> PostgreSQL/Redis (same instances as above - shared connection pools/semaphores)
```

**The critical architectural fact**: LiveKit's WebRTC/audio traffic never touches nginx or `ptai.service`. The browser connects directly to LiveKit Cloud; `ptai.service`'s only LiveKit-related role is minting short-lived join tokens via `/api/`. `ptai-livekit-agent.service` connects to LiveKit Cloud independently, as its own registered worker.

### Services

| Service | Unit | Role |
|---|---|---|
| `ptai.service` | systemd | FastAPI backend, 3 Uvicorn workers |
| `ptai-livekit-agent.service` | systemd | LiveKit agent worker (voice interviews) |
| `nginx.service` | systemd (OS package) | TLS termination, static frontend, reverse proxy |
| `redis-server.service` | systemd (OS package) | Distributed concurrency semaphores (`interview_slot`/`tts_slot`) |
| `postgresql.service` | systemd (OS package) | Application database |

### Directories

| Path | Contents |
|---|---|
| `/home/ubuntu/AI_Patient` | Git checkout **and** runtime source for both Python services — the repository IS the deployment (no release/artifact separation today) |
| `/home/ubuntu/AI_Patient/backend/.venv` | FastAPI's virtualenv |
| `/home/ubuntu/AI_Patient/backend/.venv-livekit` | LiveKit worker's virtualenv (deliberately separate from FastAPI's) |
| `/home/ubuntu/AI_Patient/dist` | Frontend build output (built in the repo checkout) |
| `/var/www/ptai` | nginx's actual served frontend root — a **copy** of `dist/` (confirmed byte-identical content and matching mtime, not a symlink) |
| `/home/ubuntu/AI_Patient/backend/.env` | Shared environment file for **both** `ptai.service` and `ptai-livekit-agent.service` (`EnvironmentFile=`) |

### Python versions (verified — see "Python Version Mismatch" below for full analysis)

| Environment | Version | How it was created |
|---|---|---|
| CI (`.github/workflows/ci.yml`) | 3.12 | Explicit pin |
| `backend/Dockerfile` | 3.12 (`python:3.12-slim`) | Explicit pin |
| Production `backend/.venv` (FastAPI) | **3.14.4** | Plain OS system Python (`/usr/bin/python3 -m venv`) — Ubuntu 26.04's default `python3` |
| Production `backend/.venv-livekit` (LiveKit) | **3.13.15** | A `uv`-managed standalone interpreter (`~/.local/share/uv/python/cpython-3.13.15-.../bin/python3.13 -m venv`) |

### Known Git-state facts (as of last inspection)

- Production `HEAD`: `5f7ee0eb026461b749729d3915f056ea3f824c8b` ("Fix Camden caregiver voice routing in LiveKit")
- `origin/main` (GitHub): 2 commits ahead — the two Phase 1 CI commits, intentionally not deployed
- `package-lock.json` has a local modification consisting only of npm-version metadata noise (harmless, but a real divergence from the committed lockfile)
- `.env.production` is untracked, contains only `VITE_API_BASE_URL` (the frontend build-time API URL — not a backend secret), and is **not currently covered by `.gitignore`** (a latent risk: a future `git add -A` could accidentally stage it — it has never actually been committed)

---

## FUTURE architecture (proposed — nothing below is implemented)

```
GitHub
   |
   v
GitHub Actions CI  (exists today - backend-tests, frontend-tests-build)
   |
   v
GitHub Actions CD  (does not exist yet)
   |
   v
production-preflight.sh   (exists today as a script; not yet wired into any CD workflow)
   |
   v
EC2 (same single host, until Task 9's multi-worker scaling triggers)
 ├── nginx            (unchanged)
 ├── frontend          /opt/ptai/current/dist  (proposed - see release-directory design)
 ├── ptai.service      /opt/ptai/current/backend  (proposed)
 ├── ptai-livekit-agent.service  /opt/ptai/current/backend  (proposed)
 ├── PostgreSQL        (unchanged)
 └── Redis             (unchanged)
```

See the accompanying Phase 2 report for the full design discussion (authentication mechanism, release-directory layout, change-aware deployment classification, LiveKit drain strategy, migration policy, rollback design) — none of it is implemented by this document or by `scripts/production-preflight.sh`.

---

## Security observations (documented here, not fixed — see Phase 2 report for severity classification)

- `/docs` and `/openapi.json` are publicly proxied by nginx (FastAPI's auto-generated Swagger UI/OpenAPI schema).
- `.env.production` is untracked but not `.gitignore`-covered.
- Redis has no `requirepass` — relies entirely on its `127.0.0.1`-only bind for isolation.
- Production Python versions diverge from CI/Dockerfile on both services.
