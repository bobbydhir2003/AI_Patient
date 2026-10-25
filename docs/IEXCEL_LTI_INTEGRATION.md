# AI iEXCEL Course 1 integration (LTI 1.3 tool)

The AI Standardized Patient can be opened as **Course 1** from the AI iEXCEL platform. The platform signs students and course instructors in through an LTI 1.3 launch. This app reports minimal progress back through the iEXCEL Events API. The two applications keep separate databases and share no code.

**It is off by default** (`AI_SP_LTI_ENABLED=false`). Then:
- every `/api/lti` route answers 404
- course tokens are refused
- nothing is reported

Direct login, existing accounts, sessions, transcripts, assessments, and surveys are unchanged in every mode.

The full cross-application description, with architecture, event schema, verification and open decisions, is in the platform repository: `docs/integration/ai-sp-course1-implementation.md`.

## What a launch does here

1. **Login.** `GET|POST /api/lti/login` (OIDC third-party login) checks the platform parameters. It stores a single-use state and nonce, binds the state to the browser with an HttpOnly cookie, and redirects to the platform.
2. **Launch.** `POST /api/lti/launch` validates the platform's RS256 `id_token`:
   - the JWKS key (`kid`)
   - `iss`, `aud`/`azp`, `exp`, `nonce`
   - deployment, message type, LTI version, and `iexcel_contract_version`
   - role, resource link, and course context
3. **Identity.** The launch maps (issuer, opaque platform subject) to an AI SP account. The first launch creates a new account: role `student`, no password, placeholder email `lti-…@lti.invalid`. Accounts are **never** matched by email, name, or student number.
4. **Code exchange.** The browser lands on `/lti/callback#code=…` (a one-time, 60-second code in the URL fragment). The web app removes it from the address bar and exchanges it once (`POST /api/lti/session/exchange`) for a course session token. That token is kept in `sessionStorage`, never `localStorage`.

## Course scoping

- **Learners:**
  - They use the normal interview flow, limited to their own sessions in the launched offering.
  - They can start only the cases assigned to that offering: `context_case_assignments` (Camden, Carly, Sofia, Jayden).
  - New sessions carry `interview_sessions.context_id`. Every existing session keeps NULL and is never reported.
- **Course instructors:**
  - They use `/api/lti/instructor/*` for the launched offering only:
    - roster
    - interviews
    - assessment summaries
    - case assignment
    - transcripts, only with `LTI_INSTRUCTOR_TRANSCRIPTS_ENABLED=true`
  - They never get AI SP administrator access.

## Progress reporting

- `activity.started`, `activity.completed`, and `assessment.completed` are written to `platform_event_outbox` in the same transaction as the change.
- Each has a deterministic event id.
- A background worker delivers them with a signed client assertion and retries with backoff.
- The verified qualitative `overall_level` is included. Nothing else from the assessment is sent: no transcript, narrative, survey answer, name, or numeric score.

## Configuration

See the `AI iEXCEL Course 1 integration` block in `backend/.env.example`. The platform values come from its application registry. For local development, the platform's `scripts/course1/up.sh` produces and applies all of them.

- **Tool key:** `python -m scripts.generate_lti_tool_key` creates a development key in `backend/.lti/` (gitignored). Production keys are provisioned separately.
- **Fake AI:** `AI_SP_DEV_FAKE_AI=true` (refused in production and staging) replaces OpenAI with synthetic outputs. It also adds a "Run simulated interview" action instead of the LiveKit voice interview, so the flow runs on a laptop.

## Migrations

- `iexcel_lti_01` (from `0020`) adds seven tables and one nullable column.
- `iexcel_lti_02` merges it with `0021`.
- An environment at `0020` can target `iexcel_lti_01` without running `0019`.
- The ids are non-numeric so they cannot collide with `0022`–`0027` on `integrate-realtime-cleanup`.

## Rollback

Set `AI_SP_LTI_ENABLED=false` and restart. No data is deleted. Launched accounts and course sessions stay readable by AI SP administrators, as before.

## Tests

`backend/tests/test_lti_integration.py` and `backend/tests/test_lti_migration.py`.
