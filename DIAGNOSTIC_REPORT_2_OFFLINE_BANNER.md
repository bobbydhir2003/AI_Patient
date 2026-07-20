# Diagnostic Report — "Not connected to the interview backend"

Date: 2026-07-14 · Diagnosis only. **No files were modified.**
Method note: my sandbox cannot reach your `localhost`, so live HTTP probes are marked
**[verify locally]** with exact commands. Everything else below is read directly from the
current files on disk and from `backend/ptai_dev.db`.

---

## A. Executive summary

The frontend and backend URLs match perfectly — this is **not** a routing, `/api/v1`,
port, env-var, or CORS problem. The root cause is a **stale SQLite schema**: the running
database `backend/ptai_dev.db` was created on Jul 13 (phase-1 schema) and was never
migrated, while the phase-2 backend models now require new columns
(`interview_sessions.active_topic`; `conversation_turns.model_name`, `prompt_version`,
`facts_used`, `response_type`, `validation_status`). `AUTO_CREATE_TABLES=true` only
creates *missing tables* — it never alters existing ones — and Alembic has never run
against this DB (there is no `alembic_version` table). Consequently `GET /api/health`
succeeds (plain `SELECT 1`) and `/docs` loads, but **`POST /api/sessions` throws a
SQLAlchemy `OperationalError` ("table interview_sessions has no column named
active_topic") → HTTP 500** → the frontend's `createSession()` catch sets the connection
state to `offline` and shows the banner. The `{"detail":"Not Found"}` response is FastAPI's
default 404 for a URL with no route (almost certainly `http://localhost:8000/` typed in
the browser) and is harmless — the frontend never calls the root. A secondary issue:
`app/main.py` was edited at 01:08 and the ASGI instance was renamed `app` →
`application`, which breaks the documented run command and the Dockerfile (see §J).

## B. Exact frontend URLs (resolved)

No frontend `.env` / `.env.local` / `.env.development` exists — only `.env.example`
(not loaded by Vite). Therefore `VITE_API_BASE_URL` is **undefined** and
`src/services/api.ts:8-9` falls back to the default. Every request is built as
`` `${API_BASE_URL}/api${path}` `` with `API_BASE_URL = http://localhost:8000` — there is
no `/api/api` duplication:

| Operation | Final URL |
|---|---|
| Health (exported but **never called by the UI**) | `GET http://localhost:8000/api/health` |
| Case list | `GET http://localhost:8000/api/cases` |
| Case details (unused by UI) | `GET http://localhost:8000/api/cases/{caseId}` |
| Create session | `POST http://localhost:8000/api/sessions` |
| Resume session | `GET http://localhost:8000/api/sessions/{sessionId}` |
| Send message | `POST http://localhost:8000/api/interviews/{sessionId}/messages` |
| Complete session | `POST http://localhost:8000/api/sessions/{sessionId}/complete` |

## C. Exact backend routes (read from `app/main.py` + `app/api/*` — all routers registered with `prefix="/api"`)

```
GET  /api/health
GET  /api/cases
GET  /api/cases/{case_id}
POST /api/sessions                            (201)
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/complete
POST /api/interviews/{session_id}/messages
```
There is no `/api/v1`, and no root `/` route (hence §I). All four `include_router` calls
are present in `create_app()`.

## D. Mismatch table

| Operation | Frontend URL | Backend route | Match? |
|---|---|---|---|
| Health | `/api/health` | `GET /api/health` | ✅ |
| Cases | `/api/cases` | `GET /api/cases` | ✅ |
| Create session | `POST /api/sessions` | `POST /api/sessions` | ✅ |
| Resume session | `GET /api/sessions/{id}` | `GET /api/sessions/{id}` | ✅ |
| Send message | `POST /api/interviews/{id}/messages` | `POST /api/interviews/{id}/messages` | ✅ |
| Complete session | `POST /api/sessions/{id}/complete` | `POST /api/sessions/{id}/complete` | ✅ |

Request/response field names also match: the backend schemas use camelCase aliases
(`studentName`, `studentId`, `caseId` → `SessionCreateRequest`; responses expose
`sessionId`, `turnId`, `patientText`). **Zero URL or schema mismatches.**

## E. Health-check result

`GET http://localhost:8000/api/health` works (you reported `database: connected`, which is
consistent: it only runs `SELECT 1`, touching no ORM columns). Crucially, **the interview
page does not use the health endpoint at all** — `fetchHealth` exists in `api.ts` but has
no caller. A green health check therefore says nothing about session creation.

## F. Session-creation result

This is where the flow breaks, at the **database write** step:

```
Interview page loads                                   ✅
→ createSession() called with correct camelCase body   ✅ (studentName/studentId/caseId)
→ POST /api/sessions reaches FastAPI                   ✅ (route exists)
→ schema validates                                     ✅
→ StudentRepository SELECT on students                 ✅ (students table unchanged)
→ InterviewSession INSERT                              ❌ 500 - OperationalError:
     the model includes active_topic, but ptai_dev.db's interview_sessions
     table (created Jul 13, phase-1) has columns only:
     [id, student_id, case_id, status, locked, disclosed_fact_ids, started_at, completed_at]
→ frontend catch → setConnection("offline") → banner   (correct behavior, real error)
```

Verified from the DB file itself: `conversation_turns` is likewise missing
`model_name/prompt_version/facts_used/response_type/validation_status`, there is **no
`alembic_version` table** (migrations never ran), and the DB has 0 students / 0 sessions /
0 turns — no session has ever been created. **[verify locally]**:

```bash
curl -i -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"studentName":"Test","studentId":"","caseId":"carly"}'
# expect: 500, and the uvicorn console shows
# sqlalchemy.exc.OperationalError: table interview_sessions has no column named active_topic
```

## G. CORS result

Not involved. Backend allowlist is `http://localhost:5173, http://127.0.0.1:5173`
(`backend/.env` + `config.cors_origin_list`), and `vite.config.ts` pins
`port: 5173, strictPort: true`, so the dev origin cannot drift to 5174. No trailing-slash,
protocol, or wildcard-credentials conflicts. (A CORS block would also not produce a JSON
500/404 body in the browser console the way observed.)

## H. Environment result

Frontend: no env file present → `VITE_API_BASE_URL` unset → code default
`http://localhost:8000` (correct). Nothing stale to reload, though note Vite only reads
env files at startup if one is added later. Backend: `backend/.env` →
`DATABASE_URL=sqlite:///./ptai_dev.db`, `AUTO_CREATE_TABLES=true`, OpenAI key set (166
chars, redacted), model `gpt-4o-mini`, port comes from the uvicorn command (expected 8000).

## I. "Not Found" explanation

`{"detail":"Not Found"}` is FastAPI's **default 404 body**, produced for any path with no
route. The frontend never requests `/`, and all its real URLs exist (§D), so this response
came from a manual browser visit — almost certainly `http://localhost:8000/` (no root
route is defined), or a variant like `/health` or `/api/v1/...`. It is **harmless** and is
not the frontend's health check. (App-level errors from my handlers look different:
`{"error": {"code": ..., "message": ...}}`.)

## J. Root cause

1. **Primary — stale DB schema:** `backend/ptai_dev.db` (created 2026-07-13 15:51,
   phase-1) lacks the phase-2 columns required by `app/models/interview_session.py:28`
   and `app/models/conversation_turn.py:30-34`. Migration
   `app/database/migrations/versions/0002_turn_metadata_active_topic.py` was never applied
   (no `alembic_version` table), and `Base.metadata.create_all` (enabled by
   `AUTO_CREATE_TABLES=true`) cannot add columns to existing tables. Every
   `POST /api/sessions` → 500 → `InterviewPage.tsx` `initializeInterview` catch →
   `setConnection("offline")`. The banner is truthful.
2. **Secondary — ASGI entrypoint rename:** `app/main.py` was modified today at 01:08:
   `app = create_app()` became `application = create_app()`. The README and the
   `Dockerfile` `CMD` both reference `app.main:app`, which no longer exists — running the
   documented command now fails with *"Attribute 'app' not found in module app.main"*.
   Since your server responds on :8000, it must currently be launched as
   `app.main:application` (or is an older process). This did not cause the Offline banner,
   but it will bite anyone following the README/Dockerfile. **[verify locally]** which
   command your uvicorn was started with.

Frontend state handling itself is correct: the banner is driven by
`fetchSession`/`createSession` (lines ~120-160 of `InterviewPage.tsx`), Retry re-runs the
same initialization and flips to `connected` on success, and Send stays disabled until a
valid session exists for the route's case.

## K. Files that would need modification (when you approve the fix — none touched)

- `backend/ptai_dev.db` — delete (it is empty: 0 rows in all tables) **or** migrate
  (`alembic stamp 0001` then `alembic upgrade head`).
- `backend/app/main.py` — restore `app = create_app()` (or keep `application` and update
  the two references below; restoring `app` is the smaller change).
- `backend/Dockerfile` + `backend/README.md` — only if `application` is kept.

## L. Recommended fix order (not implemented)

1. Stop the backend; since the dev DB contains zero data, delete `backend/ptai_dev.db`
   (or run `alembic stamp 0001 && alembic upgrade head` if you prefer migrations).
2. Align the ASGI entrypoint: restore `app = create_app()` in `app/main.py` so
   `uvicorn app.main:app --reload --port 8000`, the README, and the Dockerfile all agree.
3. Start the backend (with `AUTO_CREATE_TABLES=true` the fresh DB gets the full phase-2
   schema) and confirm the curl in §F returns **201** with a `sessionId`.
4. Reload the interview page: badge should read **Connected**; send "Hi" and confirm a row
   pair appears in `conversation_turns` with `model_name`/`prompt_version` populated.
