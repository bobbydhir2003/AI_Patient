# Authentication & Admin Panel — Implementation Notes

This document summarises the secure login system and admin panel added to the
existing PT AI Patient Simulator, without changing the patient interview or
assessment workflows.

## 1. The existing system (as found)

**Backend** — FastAPI + SQLAlchemy 2.0 + Alembic, organised into
`app/api` (routers), `app/services`, `app/repositories`, `app/schemas`,
`app/models`, `app/core`, `app/database`. Pydantic schemas serialise to
camelCase (`CamelModel`). Domain errors derive from `AppError` and are turned
into `{"error": {"code", "message"}}` JSON. Default DB is PostgreSQL; tests and
local dev use SQLite. Existing data (in `backend/ptai.db`): **8 students,
62 interview sessions, 312 conversation turns, 24 assessment runs** with domain
results and evidence.

Data model relationships:
`Student 1─* InterviewSession 1─* ConversationTurn`, and
`InterviewSession 1─* AssessmentRun 1─* AssessmentDomainResult 1─* AssessmentEvidence`,
where each evidence row references the `ConversationTurn` it is anchored to.
Assessments use **qualitative levels** (Advanced / Proficient / Developing /
Needs Improvement / Insufficient Evidence), not numeric scores.

**Frontend** — React 19 + react-router-dom 7 + Vite + TypeScript, CSS modules,
a REST client in `src/services/api.ts`, and app state in
`src/state/AppContext.tsx`. Dark UNMC theme via CSS variables in
`src/styles/variables.css`.

## 2. What was added

A JWT-based auth layer, role-based access control (student / admin), a student
self-service dashboard, and a full admin panel with search/filter/sort/
pagination, safe archive + permanent-delete flows with cascade handling, and an
append-only audit log. Existing routes, models, and the interview/assessment
pipeline were left untouched.

### Security
- Passwords hashed with **bcrypt** (`app/core/security.py`); plain text is never
  stored. Login returns one generic `invalid_credentials` error for both unknown
  email and wrong password, and runs a dummy hash verify to keep timing uniform.
- **JWT** access tokens (HS256) carry `sub` (user id) + `role` with an expiry.
  Secret comes from `JWT_SECRET_KEY`.
- RBAC + ownership enforced in `app/dependencies/auth.py`: `get_current_user`,
  `require_admin`, `require_student`, `require_session_access` (a student may
  only reach their own sessions; another student's session returns 404 so its
  existence is not revealed; admins may reach any).
- Admin cannot deactivate/delete their own account; permanent deletion requires
  the body `confirm: "DELETE"`.

### Endpoints
Auth: `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`,
`POST /api/auth/logout`.
Student: `GET /api/students/me`, `/me/sessions`, `/me/sessions/{id}`,
`/me/sessions/{id}/transcript`, `/me/sessions/{id}/assessment`.
Admin (all require an admin token): `GET /api/admin/dashboard`,
`/admin/students`, `/admin/students/{id}`, `/admin/students/{id}/sessions`,
`/admin/sessions`, `/admin/sessions/{id}`, `/admin/sessions/{id}/transcript`,
`/admin/sessions/{id}/assessment`, `/admin/assessments/{id}`,
`/admin/audit-logs`;
mutations `PATCH /admin/students/{id}/status`, `DELETE /admin/students/{id}`,
`PATCH /admin/sessions/{id}/archive`, `DELETE /admin/sessions/{id}`,
`DELETE /admin/assessments/{id}`, `DELETE /admin/messages/{id}`.

### Cascade + audit
Destructive actions run in one transaction and write an `AuditLog` row before
commit. Deletion order avoids orphaned rows on both SQLite and PostgreSQL:
assessment runs (which cascade to domain results + evidence) are removed before
the sessions/turns they reference; deleting a student removes assessments →
sessions/turns → user account → student profile.

## 3. Files created

**Backend**
```
app/core/security.py
app/models/user.py
app/models/audit_log.py
app/repositories/user_repository.py
app/repositories/audit_repository.py
app/schemas/auth.py
app/schemas/admin.py
app/dependencies/__init__.py
app/dependencies/auth.py
app/services/auth_service.py
app/services/admin_service.py
app/api/auth.py
app/api/admin.py
app/api/students.py
app/database/migrations/versions/0007_auth_users_and_audit_log.py
scripts/__init__.py
scripts/create_admin.py
tests/test_auth.py
tests/test_admin.py
```

**Frontend**
```
src/services/authApi.ts
src/state/AuthContext.tsx
src/portal/portal.css
src/portal/ui.tsx
src/portal/ProtectedRoute.tsx
src/portal/TranscriptView.tsx
src/portal/AssessmentDisplay.tsx
src/portal/format.ts
src/pages/auth/LoginPage.tsx
src/pages/auth/RegisterPage.tsx
src/pages/student/StudentDashboardPage.tsx
src/pages/student/StudentSessionPage.tsx
src/pages/admin/AdminLayout.tsx
src/pages/admin/AdminDashboardPage.tsx
src/pages/admin/AdminStudentsPage.tsx
src/pages/admin/AdminStudentDetailPage.tsx
src/pages/admin/AdminSessionsListPage.tsx
src/pages/admin/AdminSessionPage.tsx
src/pages/admin/AdminAuditLogPage.tsx
```

## 4. Files modified

**Backend**: `app/core/config.py` (JWT + admin settings),
`app/core/constants.py` (roles, audit action types, `archived` status),
`app/core/exceptions.py` (auth/admin exceptions),
`app/models/__init__.py` (register `User`, `AuditLog`),
`app/models/student.py` (added `email`, `is_active`, `user` relationship),
`app/main.py` (wire `auth`, `students`, `admin` routers),
`requirements.txt` (added `bcrypt`, `PyJWT`), `.env.example` (JWT + admin vars).

**Frontend**: `src/main.tsx` (wrap in `AuthProvider`, import `portal.css`),
`src/App.tsx` (auth / student / admin routes; header hidden in admin area).

## 5. Database migration

The change is additive — **no existing data is deleted**. Migration `0007`
adds the `users` and `audit_logs` tables and two columns to `students`
(`email` default `''`, `is_active` default `true`). Existing students are
preserved and treated as active profiles with no login account yet.

```bash
cd backend
# PostgreSQL: DATABASE_URL is read from the environment / .env
alembic upgrade head
# SQLite local dev:
DATABASE_URL="sqlite:///./ptai.db" alembic upgrade head
```

Verified on a copy of the real `ptai.db`: all 8 students, 62 sessions,
312 turns and 24 assessments remain intact after upgrade. (A `ptai.db.pre-0007.bak`
backup was created during development; the live `ptai.db` is unchanged and still
needs `alembic upgrade head` run once on your machine.)

**Activating legacy students:** existing students have no password. They
activate by registering at `/register` with their **student number** — the
backend links the new account to the matching existing `Student` profile
(preferring one without an account), so their prior sessions and assessments
appear immediately. No password is ever auto-generated.

## 6. Backend start commands

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # first time
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

## 7. Frontend start commands

```bash
npm install
npm run dev            # http://localhost:5173
# production build:
npm run build && npm run preview
```

## 8. Admin account setup (no hard-coded password)

Set the secret and admin bootstrap values in `backend/.env` (git-ignored) or the
shell, then run the seed command. `.env.example` documents every variable but
contains no real secrets.

```bash
cd backend
# generate a strong JWT secret once and put it in backend/.env:
python -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_urlsafe(48))"

ADMIN_EMAIL=admin@school.edu ADMIN_PASSWORD='choose-a-strong-password' \
  python -m scripts.create_admin
# or:
python -m scripts.create_admin --email admin@school.edu --password 'strong-pass'
```

Re-running promotes an existing account to admin and resets its password, so it
is safe to repeat. Sign in at `/login`; admins land on `/admin`, students on
`/student/dashboard`.

## 9. Test commands

```bash
cd backend
pytest                       # full suite
pytest tests/test_auth.py tests/test_admin.py    # the new auth/admin tests
```

New coverage (25 tests): student registration + password hashing, student/admin
login, generic invalid-login, student→admin blocked, unauthenticated admin
blocked, student cannot read another student's session, admin views
(dashboard / students / search / transcript / assessment), archive + reactivate,
archive session, delete session (cascade), delete assessment only, delete-message
evidence cleanup, permanent student delete requires confirmation, full cascade
delete, admin cannot delete own account, and audit-log creation.

Result: **160 passed**. One unrelated test
(`test_streaming.py::test_disabled_by_default_returns_409`) fails only in the
Linux verification sandbox because of a newer Starlette version's SSE exception
handling; it is not affected by this work and passes on the project's pinned
dependencies.

## 10. Remaining limitations / notes

- **Stateless JWT logout.** `POST /auth/logout` is a client-side token discard;
  there is no server-side token blocklist. If revocation is needed, add a
  denylist or shorten `ACCESS_TOKEN_EXPIRE_MINUTES`.
- **Token storage.** The frontend stores the JWT in `localStorage` for
  simplicity. For stricter XSS posture, move to http-only cookies.
- **Deleting a transcript message leaves a turn-index gap** (indices are not
  re-sequenced) to preserve idempotency keys used by the interview engine. This
  is intentional; the action is meant for rare corrections and is confirm-gated.
- **First registered student** links to a matching legacy `Student` by number;
  if two legacy profiles share a number, the one without an account is chosen.
- Admin session-archive sets status `archived` and locks the session; archived
  sessions are excluded from the "incomplete" dashboard count.
- The verification sandbox cannot run `vite build`/`oxlint` (Linux-native
  binaries absent) — TypeScript type-checking passes (`tsc -p tsconfig.app.json
  --noEmit`, exit 0); run `npm run build` on your machine for the bundle.
```
