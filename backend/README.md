# PT AI Patient Simulator - Backend

FastAPI backend for the UNMC PT AI Patient Simulator. Structured patient cases,
interview sessions, OpenAI-generated patient responses, transcript persistence,
and session locking. **No assessment/scoring yet.**

## Core principle

> No backend or OpenAI connection = no patient conversation.

OpenAI is the ONLY generator of patient dialogue. There are no canned replies,
no fact-text-as-dialogue fallbacks, and no mock mode. If generation fails after
the configured retries, the API returns:

```json
{"error": {"code": "PATIENT_RESPONSE_UNAVAILABLE",
           "message": "The patient response could not be generated. Please try again."}}
```

(HTTP 503). Nothing is persisted for the failed turn; the student retries.

## Stack
Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · PostgreSQL · OpenAI SDK (Responses API, structured output) · pytest

## Setup

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL and OPENAI_API_KEY
alembic upgrade head          # includes migration 0002 (turn metadata, active topic)
uvicorn app.main:app --reload --port 8000
```

An OpenAI API key is REQUIRED for interviews. Without it, message turns return
503 PATIENT_RESPONSE_UNAVAILABLE by design.

## Endpoints (prefix `/api`)
| Method | Path | Purpose |
|---|---|---|
| GET | /health | Liveness + DB connectivity |
| GET | /cases | List the four patient cases |
| GET | /cases/{caseId} | One case (student-visible info only) |
| POST | /sessions | Create a session `{studentName, studentId?, caseId}` |
| GET | /sessions/{id} | Session + full transcript |
| POST | /sessions/{id}/complete | Complete & lock (idempotent) |
| POST | /interviews/{id}/messages | `{text, caseId}` → `{turnId, patientText, status, sessionStatus, speech}` |
| GET | /voice/status/{caseId} | Whether the ElevenLabs patient voice is available for the case |
| POST | /voice/synthesize | Stream ElevenLabs audio for an approved patient turn |

Guards: locked session → 409 `session_locked`; `caseId` mismatch with the
session's case → 409 `case_session_mismatch` (cross-case isolation).

## Patient voice (ElevenLabs)

ElevenLabs is the patient's VOICE only - OpenAI remains the only generator of
patient dialogue. Set `ELEVENLABS_API_KEY` in `.env` and paste each character's
voice ID into `voice_profile.voice_id` in `app/cases/*.json`. Without a key or
voice ID the app falls back to browser speechSynthesis automatically. Full
guide: [`../VOICE_INTEGRATION.md`](../VOICE_INTEGRATION.md).

## Patient engine
`topic_classifier` (incl. presenting-concern, body parts, follow-up detection)
→ `context_resolver` (recent turns + active topic) → `fact_selector` →
`disclosure_manager` (open/probe/sensitive, capped per turn) → `prompt_builder`
(persona + eligible facts, already-shared markers) → `openai_client`
(Responses API, strict JSON schema: `patient_text`, `used_fact_ids`,
`response_type`, `supported`) → `response_validator` (character breaks,
cross-case name leakage) → result. `fallback_manager` implements ONLY the
retry/error policy - it contains no dialogue.

Each patient turn stores: model_name, prompt_version, facts_used,
response_type, validation_status. Per-turn logs include session_id, case_id,
turn, topic, model, validation and save status (never API keys).

## Case files (`app/cases/*.json`)
Structured truth only: profile, persona/speech style, facts with topic +
disclosure level, PAVING scores, source references, documented data gaps.
No scripted conversations, no question→answer pairs (enforced by tests).

## Tests

```bash
pytest   # SQLite + fake OpenAI boundary; includes case-isolation, OpenAI-failure,
         # case/session-mismatch, and a static scan proving the React production
         # code contains no mock-conversation paths
```
