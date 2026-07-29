# PT AI Patient Simulator

An AI-powered physical therapy patient interview and assessment simulation platform developed at the University of Nebraska Medical Center (UNMC) College of Allied Health Professions. Students practice health-promotion interviewing by conversing with realistic AI-simulated patients, then receive structured rubric-based assessments of their clinical communication skills.

---

## Purpose

Physical therapy education requires repeated practice of patient interviewing in realistic clinical scenarios. This platform provides on-demand simulated patient encounters where students can:

- Conduct health-promotion interviews with AI patients across diverse clinical cases
- Practice motivational interviewing techniques in a safe learning environment
- Receive structured, rubric-based feedback on their interview performance
- Review full conversation transcripts and assessment results at their own pace

Faculty and administrators can monitor student activity, review transcripts and assessments, manage system configuration, and oversee AI service health through a dedicated admin dashboard.

---

## Main Features

- **AI-simulated patient conversations** powered by OpenAI (GPT-4o-mini / GPT-4o)
- **Realistic patient voices** via ElevenLabs text-to-speech with per-case voice profiles
- **Sentence-level audio streaming** for low-latency conversational pacing
- **Multi-participant interviews** where a parent or caregiver participates alongside the patient
- **PAVING Wheel wellness visualization** (12-category radar chart) for each patient case
- **Dual assessment system** supporting standard interview rubrics and referral/interprofessional cases
- **Full transcript recording** with speaker identity, turn indexing, and topic classification
- **Role-based access control** with student, admin, and super_admin tiers
- **Runtime AI configuration** — editable OpenAI model, ElevenLabs voice, and conversation settings without redeployment
- **Encrypted credential storage** for API keys managed through the admin UI
- **Admin notification feed** derived from real audit-log events
- **System health dashboard** monitoring backend, database, OpenAI, ElevenLabs, and storage status

---

## Student Workflow

1. **Sign in or register** — Students create an account or sign in with existing credentials.
2. **Browse case catalog** — Cases are organized into Standard PT Cases and Referral & Interprofessional Cases.
3. **Review case introduction** — Each case presents patient demographics, medical history, referral reason, student-visible information, the PAVING Wheel, and the interview task.
4. **Conduct the interview** — Students interact in real time with the AI patient through a chat interface with optional voice playback.
5. **Complete the session** — The session locks and triggers AI-powered assessment.
6. **Review results** — Students access their transcript and structured assessment with category-level rubric scores.

---

## Admin and Super-Admin Capabilities

### Admin (`admin` role)

- View the Academic Dashboard (student counts, session statistics, assessment distributions)
- Search and browse all students, sessions, transcripts, and assessments
- Archive or delete sessions, students, assessments, and individual transcript messages
- View and edit AI configuration (OpenAI model, ElevenLabs settings, conversation parameters)
- Edit patient voice profiles (stability, similarity, speed, style)
- Run API connection tests for OpenAI and ElevenLabs
- View the System Dashboard with real-time health checks
- View the notification feed and audit log

### Super Admin (`super_admin` role)

Everything an admin can do, plus:

- Replace or remove stored API credentials (OpenAI key, ElevenLabs key)
- Restore configuration to previous versions from the change history
- Clear the audio cache

---

## Available Patient Cases

### Standard PT Cases

Cases are defined as structured JSON files in `backend/app/cases/`. Each includes demographics, medical history, persona, speech behavior, voice profile, facts with disclosure levels, PAVING Wheel scores, and source-document references.

| Case | Patient | Age | Setting | Description |
|------|---------|-----|---------|-------------|
| Camden | Camden Anderson | 4 | Pediatric outpatient | A child receiving treatment for high-risk acute lymphoblastic leukemia experiencing reduced activity. Multi-participant: Camden's mother is the primary historian. |
| Carly | Carly Wishard | 38 | Outpatient physical therapy | An adult physical therapist with a recent breast cancer diagnosis balancing treatment, wrist pain, family, and work. |
| Sofia | Sofia Hernandez | 13 | Pediatric outpatient | A teenager with polyarticular juvenile idiopathic arthritis experiencing school limitations and reduced dance participation. |
| Jayden | Jayden Jackson | 45 | Outpatient physical therapy | An active adult recently diagnosed with systemic lupus erythematosus who wants to continue exercising and leading a youth running program. |

### Referral & Interprofessional Cases

These advanced cases focus on recognizing concerns that may require consultation, referral, care coordination, or escalation beyond the current PT encounter.

| Case | Patient | Age | Setting | Description |
|------|---------|-----|---------|-------------|
| Jordan | Jordan Mills | 24 | Outpatient rehabilitation | A patient with knee pain who reports reduced energy during activity. |
| Eleanor | Eleanor Whitfield | 74 | Outpatient rehabilitation | An older adult working on balance who describes occasional dizzy spells. |
| Marcus | Marcus Deyo | 61 | Neurological rehabilitation | A patient in neurological rehabilitation with reduced endurance and difficulty managing daily activities. |
| Priya | Priya Shah | 38 | Outpatient chronic pain program | A patient with persistent pain and poor sleep who has stepped back from work and social activities. |

---

## Multi-Participant Patient Interaction

The Camden case introduces multi-participant conversations where the patient (age 4) is accompanied by his mother, who serves as the primary historian.

A deterministic **speaker router** (`backend/app/patient_engine/speaker_router.py`) decides who should respond before any model call:

- Questions about medical history, medications, or logistics route to the mother
- Questions using Camden's name or child-appropriate cues route to Camden
- "Both of you" phrasing routes to both participants
- Short follow-ups stay with the active speaker

A **child response validator** (`backend/app/patient_engine/child_response_validator.py`) ensures Camden's responses stay age-appropriate:
- Responses exceeding 25 words are truncated to the nearest sentence boundary
- Clinical language a 4-year-old would not use triggers a safe deflection to the mother

Speaker identity (speaker ID and display label) is persisted per conversation turn and surfaced in transcripts and assessment rubrics.

---

## PAVING Wheel Visualization

The PAVING Wheel is a 12-category wellness assessment tool used in physical therapy education. Each patient case includes scored PAVING profile data sourced from scanned patient worksheets.

**Categories** (each scored 0–25):
Physical Activity, Attitude, Variety, Investigations, Nutrition, Goals, Stress Management, Time Outs, Energy, Purpose, Sleep, Social Connections

The frontend renders this as an interactive SVG radar chart (`src/components/cases/PavingRadar.tsx`) within a modal component (`src/components/cases/PavingWheel.tsx`). The modal includes:

- The patient-specific radar chart with per-category color coding
- A separate instructional example chart (patient-independent) with annotated callout arrows
- The original scanned PAVING Wheel image for reference (`public/patients/paving/`)
- Descriptive text explaining how to use the wheel during the interview

---

## AI Patient Conversation System

Patient responses are generated using the OpenAI API with structured JSON output:

- **Topic classification** identifies the interview topic for each student question
- **Fact-based disclosure control** determines what information the patient shares based on disclosure level (open, probe, sensitive)
- **Persona-consistent speech** uses per-case persona definitions, speech style rules, and emotional topic mappings
- **Streaming responses** deliver sentence-level text chunks for low-latency conversational flow
- **Sentence-level TTS pipelining** synthesizes audio for each sentence as it arrives

Feature flags control streaming behavior:
- `OPENAI_PATIENT_STREAMING_ENABLED` — master switch for streamed text
- `PATIENT_SENTENCE_PIPELINING_ENABLED` — audio synthesis per sentence vs. after final commit

---

## Voice Generation and Audio Streaming

Each patient case defines an ElevenLabs voice profile with tuned parameters (voice ID, model, stability, similarity boost, style, speed, speaker boost). The backend proxies all TTS requests — the ElevenLabs API key never reaches the browser.

- Audio is cached in a bounded in-memory LRU cache to avoid re-synthesizing repeated responses
- The `X-Pause-Before-Ms` response header communicates pause timing to the frontend
- Voice previews allow admins to test unsaved voice configurations before applying them
- If ElevenLabs is disabled, the frontend falls back to browser-native speech synthesis

---

## Assessment and Transcript Functionality

After a student completes an interview:

1. The session is locked and all conversation turns are preserved
2. The transcript is formatted with speaker labels (including multi-participant identity)
3. An AI-powered assessment evaluates the interview against a structured rubric
4. Standard cases and referral cases use different assessment pipelines

Assessments produce category-level scores with descriptive feedback. Students and admins can review the full transcript and assessment results through the portal.

---

## Notification System

The admin notification feed is derived from real audit-log events — there is no fabricated data. Notifications track:

- API credential changes (replaced, removed, tested)
- AI configuration updates
- Patient voice edits and restorations
- Student and session management actions (archive, delete, reactivate)
- Assessment and transcript message deletions

The unread count reflects events newer than the admin's `notifications_read_at` timestamp. "Mark all read" advances the timestamp; new activity increments the badge again.

---

## Runtime AI Configuration and System Settings

Admins can adjust AI and voice settings at runtime through the System Dashboard without redeploying:

**AI Configuration** (admin and above):
- OpenAI model selection (from an approved allowlist)
- Streaming and timeout settings
- ElevenLabs model, output format, and timeout
- Conversation behavior flags (sentence-level streaming, disclosure control, motivational interviewing, age-appropriate language, caregiver routing)

**Patient Voices** (admin and above):
- Per-case voice parameters (stability, similarity boost, style, speed, speaker boost)
- Voice preview with unsaved settings
- Restore to default voice profile

**API Credentials** (super_admin only):
- Replace or remove OpenAI and ElevenLabs API keys
- Keys are encrypted at rest using a server-side encryption key
- Masked display — full keys are never returned to the browser
- Connection testing

**Configuration History**:
- All changes are logged with previous/new values, the admin who made the change, and timestamp
- Super admins can restore previous configurations

---

## Role-Based Access Control

| Capability | Student | Admin | Super Admin |
|------------|---------|-------|-------------|
| Conduct interviews | ✓ | | |
| View own transcripts and assessments | ✓ | | |
| View all students, sessions, transcripts | | ✓ | ✓ |
| Archive/delete sessions and students | | ✓ | ✓ |
| Edit AI configuration and patient voices | | ✓ | ✓ |
| Run API connection tests | | ✓ | ✓ |
| Replace/remove API credentials | | | ✓ |
| Restore configuration versions | | | ✓ |
| Clear audio cache | | | ✓ |

---

## Technology Stack

### Frontend
- **React 19** with TypeScript 6
- **Vite 8** build tooling
- **React Router v7** for client-side routing
- **Vanilla CSS** with CSS Modules for component-scoped styles
- **OxLint** for linting

### Backend
- **FastAPI** (Python 3.10+)
- **SQLAlchemy 2** ORM
- **Alembic** for database migrations
- **Pydantic v2** with `pydantic-settings` for configuration
- **Uvicorn** ASGI server
- **bcrypt** for password hashing
- **PyJWT** for JSON Web Token authentication

### External Services
- **OpenAI API** — patient conversation generation (GPT-4o-mini, GPT-4o, GPT-4.1-mini, GPT-4.1)
- **ElevenLabs API** — text-to-speech voice synthesis

### Database
- **SQLite** for local development
- **PostgreSQL** (via psycopg2) for production deployment

---

## Frontend Architecture

```
src/
├── components/          # Reusable UI components
│   ├── admin/           # Admin sidebar, topbar, icons
│   ├── cases/           # CaseCard, PavingWheel, PavingRadar
│   └── interview/       # MessageBubble, chat interface
├── hooks/               # Custom React hooks (voice state machine, etc.)
├── pages/               # Route-level page components
│   ├── admin/           # Admin dashboard and management pages
│   │   └── system/      # System dashboard, AI config, credentials, voices
│   ├── auth/            # Login and registration
│   └── student/         # Student portal pages
├── portal/              # Student self-service portal (transcripts, sessions)
├── services/            # API client modules
│   ├── api.ts           # Base URL, ApiError class
│   ├── authApi.ts       # Authentication and admin data endpoints
│   ├── systemApi.ts     # System dashboard endpoints
│   └── runtimeApi.ts    # Runtime configuration endpoints
├── state/               # React context providers (AuthContext)
├── styles/              # Global CSS
└── types/               # TypeScript type definitions
```

---

## Backend Architecture

```
backend/
├── app/
│   ├── api/             # FastAPI route handlers
│   │   ├── auth.py      # Login, register, me, logout
│   │   ├── admin.py     # Academic dashboard, student/session CRUD
│   │   ├── admin_system.py    # System health dashboard
│   │   ├── admin_runtime.py   # Runtime AI/voice/credential config
│   │   ├── cases.py     # Case catalog endpoint
│   │   ├── sessions.py  # Session creation
│   │   ├── interviews.py      # Interview message handling
│   │   ├── assessments.py     # Assessment triggers and retrieval
│   │   ├── voice.py     # TTS audio proxy
│   │   └── health.py    # Health check
│   ├── assessment/      # Assessment pipeline (transcript prep, rubrics)
│   ├── case_assessment/ # Case-specific assessment logic
│   ├── cases/           # Patient case JSON definitions
│   ├── core/            # Config, constants, security, exceptions, logging, crypto
│   ├── database/        # SQLAlchemy base, connection, Alembic migrations
│   ├── dependencies/    # FastAPI auth dependencies (RBAC)
│   ├── models/          # SQLAlchemy ORM models
│   ├── patient_engine/  # AI response generation, topic classification,
│   │                    # speaker routing, child response validation
│   ├── prompts/         # System prompts for AI interactions
│   ├── repositories/    # Data access layer
│   ├── rubrics/         # Assessment rubric definitions
│   ├── schemas/         # Pydantic request/response schemas
│   ├── services/        # Business logic layer
│   └── voice/           # ElevenLabs client, audio cache, voice profile loading
├── scripts/             # Admin utilities (create_admin.py)
├── tests/               # Pytest test suite
├── alembic.ini          # Alembic configuration
├── requirements.txt     # Python dependencies
└── Dockerfile           # Container build definition
```

---

## Database and Alembic Migrations

The schema is managed through sequential Alembic migrations:

| Version | Description |
|---------|-------------|
| 0001 | Initial tables (students, interview sessions, conversation turns) |
| 0002 | Turn metadata and active topic |
| 0003 | Assessment tables |
| 0004 | Case category and session metadata |
| 0005 | Turn client ID and source |
| 0006 | Dual assessment mode |
| 0007 | Authentication users table and audit log |
| 0008 | Runtime configuration (API credentials, system settings, patient voice settings, configuration history) |
| 0009 | Turn speaker identity (speaker_id, speaker_label for multi-participant cases) |
| 0010 | User notifications_read_at column |

Migrations are designed to work with both SQLite (local development) and PostgreSQL (production). SQLite-incompatible operations (standalone ALTER TABLE constraints) use Alembic batch mode.

---

## Local Installation

### Prerequisites

- Node.js 18+ and npm
- Python 3.10+
- An OpenAI API key
- An ElevenLabs API key (optional; browser TTS is used as fallback)

### Clone the Repository

```bash
git clone https://github.com/bobbydhir2003/PT_AI_Assessment.git
cd PT_AI_Assessment
```

### Frontend Setup

```bash
npm install
```

Create the frontend environment file:

```bash
cp .env.example .env.local
```

The default `VITE_API_BASE_URL=http://localhost:8000` points to the local backend.

### Backend Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create the backend environment file:

```bash
cp .env.example .env
```

Edit `backend/.env` and add your API keys and a JWT secret. See [Environment Variables](#environment-variables) below.

### Apply Database Migrations

```bash
cd backend
alembic upgrade head
```

This creates the SQLite database (`backend/ptai.db`) with all tables.

### Create the First Admin Account

```bash
cd backend
ADMIN_EMAIL=admin@school.edu ADMIN_PASSWORD='your-strong-password' \
    python -m scripts.create_admin
```

Or interactively:

```bash
python -m scripts.create_admin --email admin@school.edu
```

---

## Environment Variables

### Frontend (`/.env.local`)

| Variable | Description | Example |
|----------|-------------|---------|
| `VITE_API_BASE_URL` | Backend API base URL | `http://localhost:8000` |

### Backend (`/backend/.env`)

| Variable | Description | Example |
|----------|-------------|---------|
| `ENVIRONMENT` | Runtime environment | `development` |
| `DEBUG` | Enable debug mode | `true` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///./ptai.db` |
| `AUTO_CREATE_TABLES` | Create tables without Alembic | `false` |
| `OPENAI_API_KEY` | OpenAI API key | *(your key)* |
| `OPENAI_MODEL` | Default OpenAI model | `gpt-4o-mini` |
| `OPENAI_TIMEOUT_SECONDS` | Request timeout | `30` |
| `OPENAI_MAX_OUTPUT_TOKENS` | Max output tokens | `300` |
| `OPENAI_PATIENT_MAX_OUTPUT_TOKENS` | Patient response token limit | `250` |
| `OPENAI_PATIENT_STREAMING_ENABLED` | Enable streamed patient responses | `true` |
| `PATIENT_SENTENCE_PIPELINING_ENABLED` | Sentence-level TTS pipeline | `true` |
| `ELEVENLABS_API_KEY` | ElevenLabs API key | *(your key)* |
| `ELEVENLABS_ENABLED` | Enable ElevenLabs voices | `true` |
| `ELEVENLABS_DEFAULT_MODEL` | Default TTS model | `eleven_turbo_v2_5` |
| `ELEVENLABS_OUTPUT_FORMAT` | Audio output format | `mp3_44100_128` |
| `ELEVENLABS_TIMEOUT_SECONDS` | TTS request timeout | `20` |
| `ELEVENLABS_MAX_TEXT_CHARS` | Max synthesizable text length | `1200` |
| `ELEVENLABS_CACHE_MAX_ENTRIES` | Audio cache size | `24` |
| `CORS_ORIGINS` | Allowed frontend origins | `http://localhost:5173` |
| `JWT_SECRET_KEY` | JWT signing secret | *(generate a strong random value)* |
| `JWT_ALGORITHM` | JWT algorithm | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime in minutes | `720` |
| `ALLOW_STUDENT_SELF_REGISTRATION` | Open student registration | `true` |
| `CONFIG_ENCRYPTION_KEY` | Server-side key for credential encryption | *(generate a strong value)* |
| `ADMIN_EMAIL` | Bootstrap admin email (seed script only) | `admin@school.edu` |
| `ADMIN_PASSWORD` | Bootstrap admin password (seed script only) | *(your password)* |

> **Security**: The `backend/.env` file is gitignored and must never be committed. Copy `backend/.env.example` to `backend/.env` and fill in your values. Never include real API keys, passwords, or secrets in version control.

---

## Running Locally

### Start the Backend

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

The API server starts at `http://localhost:8000`.

### Start the Frontend

In a separate terminal:

```bash
npm run dev
```

The development server starts at `http://localhost:5173` (port is pinned in `vite.config.ts` to match the backend CORS allowlist).

---

## Testing

### Backend Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/ -x -q
```

The test suite includes tests for authentication, admin endpoints, assessments, interviews, sessions, patient engine, streaming, voice integration, runtime configuration, system dashboard, notifications, PAVING profiles, multi-participant conversations, case isolation, referral cases, and more.

### Frontend Lint

```bash
npm run lint
```

### Frontend Type Check and Build

```bash
npm run build
```

### Voice State Machine Tests

```bash
npm run test:voice
```

---

## Production and Deployment Considerations

- **Database**: Switch `DATABASE_URL` from SQLite to PostgreSQL for concurrent access.
- **JWT Secret**: Generate a cryptographically strong `JWT_SECRET_KEY` (at least 32 bytes). Never use the development default.
- **Encryption Key**: Set a strong `CONFIG_ENCRYPTION_KEY` for encrypted credential storage.
- **CORS**: Update `CORS_ORIGINS` to match your production frontend URL.
- **Debug**: Set `DEBUG=false` and `ENVIRONMENT=production`.
- **HTTPS**: Deploy behind a reverse proxy (Nginx, Caddy) with TLS termination.
- **Docker**: A `Dockerfile` is included in `backend/` for containerized deployment.
- **Streaming**: Set `OPENAI_PATIENT_STREAMING_ENABLED=true` for low-latency patient responses in production.

---

## Security Notes

- Passwords are hashed using **bcrypt** and never stored in plaintext.
- API keys are **encrypted at rest** using a server-side encryption key and are never returned in full to the frontend (masked display only).
- The ElevenLabs API key **never reaches the browser**. All TTS requests are proxied through the backend.
- JWT tokens use HMAC-SHA256 signing. The signing secret must be set via environment variable and never committed.
- The `.env` files are gitignored. The `.env.example` files contain only placeholder values.
- Role-based access is enforced at the API layer through FastAPI dependencies, not just the UI.
- Unhandled exceptions return a JSON response without exposing stack traces in production.
- Admin actions are recorded in an audit log with the acting admin's identity and timestamp.

---

## Current Project Status

- 4 standard patient cases and 4 referral/interprofessional cases are implemented
- Multi-participant conversation routing is active for the Camden (pediatric) case
- Sentence-level streaming and TTS pipelining are feature-flagged and operational
- Runtime AI configuration, credential management, and system health dashboard are functional
- 10 database migrations covering the full schema
- Comprehensive test suite with 20+ test modules

---

## Future Scalability Considerations

- Additional patient cases can be added as JSON files in `backend/app/cases/` without code changes
- The multi-participant speaker routing system can be extended to other pediatric or caregiver-accompanied cases
- The referral assessment pipeline supports adding new referral domains and escalation patterns
- PostgreSQL support is in place for multi-user production deployments
- The runtime configuration system allows AI model and voice changes without redeployment
- The role system supports adding new permission tiers beyond the current three-level hierarchy

---

*Developed at the University of Nebraska Medical Center, College of Allied Health Professions.*
