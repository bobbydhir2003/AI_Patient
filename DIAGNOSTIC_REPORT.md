# PT AI Patient Simulator — Diagnostic Report

Date: 2026-07-14 · Scope: inspection, reproduction, tracing only. **No code was modified.**

Sandbox limitation disclosed up front: my audit environment cannot install Python packages
(PyPI blocked), so I could not boot uvicorn here. I verified backend behavior through
(1) your local run artifacts — `backend/ptai_dev.db` and `backend/.env` prove the backend
booted on your machine — (2) direct execution of the dependency-free engine modules
(topic classifier, fallback logic simulation), and (3) full static tracing of every
request/response path. Frontend was verified with `tsc` and code tracing; a live browser
session was not available. Items I could not observe directly are marked **[verify locally]**.

---

## A. Executive summary

| Question | Answer |
|---|---|
| Frontend and backend connected? | **No — never successfully, not even once.** `backend/ptai_dev.db` contains the three tables (so the backend booted and `AUTO_CREATE_TABLES` ran) but **0 students, 0 sessions, 0 conversation turns**. |
| Sessions created correctly? | No session has ever been created. The frontend's `createSession()` call failed every time and was **silently swallowed** by a `catch` that drops the app into mock mode. |
| Selected case preserved? | In the URL, yes. In persisted state, **no** — `messages`, `interviewStartTime`, and `sessionId` survive in localStorage across case switches and are only reset from the Assessment Results page buttons. |
| OpenAI being called? | **Never.** No sessions → no turns → no engine invocations. The key in `backend/.env` is set and the model name (`gpt-4o-mini`) is valid, but no code path has reached it. |
| Fallback responses used? | **100 % of everything you have seen is frontend mock data** (`src/data/mockResponses.ts`, cycled by `setTimeout`), plus stale localStorage transcript. The backend's own fallback manager has also never run. |
| Speech input exists? | **No.** No `SpeechRecognition` / `webkitSpeechRecognition` / `getUserMedia` anywhere in `src/`. The microphone button is a visual placebo that shows a tooltip. |
| Speech output exists? | **No.** No `speechSynthesis` / `SpeechSynthesisUtterance` anywhere. |
| Timer works? | **No.** `514:23` = ~8.6 hours elapsed since a *persisted* `interviewStartTime` from an earlier session that was never reset; the formatter also has no hour rollover. |

**The screenshot explained precisely:** "I get tired really fast when I play with my sister
now." is `mockResponses.camden[0]` (`src/data/mockResponses.ts:3`), verbatim. A Carly
interview cannot generate that line for a *new* message (mock lookup uses the URL's
`caseId`). It is a **leftover Camden message rendered from persisted localStorage state**
in the Carly interview — the same un-reset state that produced the 514:23 timer. This is
presentation-level leakage in the frontend state lifecycle, not backend cross-case
contamination (the backend has never answered anything).

---

## B. Confirmed problems

### P1 — Frontend has never reached the backend; failures are invisible · **CRITICAL**
- **Files:** `src/pages/InterviewPage.tsx` (session-create effect `.catch(() => {})`, `sendStudentMessage(...).catch(() => fallback())`, `completeSession(...).catch(() => {})`), `src/services/cases.ts:16` (`catch → mockCases`).
- **Evidence:** empty `ptai_dev.db` (tables exist, zero rows) despite the backend having booted; every catch block is empty — no `console.error`, no UI indicator.
- **Root cause:** the "graceful offline fallback" design converts *any* connection failure into silent mock mode. The user cannot distinguish "AI patient" from "canned demo."
- **Most likely connection failure triggers** (cannot be distinguished from this sandbox — **[verify locally]** in the order given):
  1. Backend not running at the same moment the app was used (DB rows would exist otherwise).
  2. **Vite port drift → CORS block:** `vite.config.ts` pins no port. If 5173 was busy, Vite serves on 5174, which is **not** in `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173` → browser blocks every request.
  3. Backend started on a port other than 8000 (frontend default base URL is `http://localhost:8000`; no `.env.local` overrides `VITE_API_BASE_URL` — only `.env.example` exists at the project root).
- **Local verification commands:**
  ```bash
  curl -s http://localhost:8000/api/health
  curl -s -X POST http://localhost:8000/api/sessions \
    -H 'Content-Type: application/json' \
    -d '{"studentName":"Test","studentId":"","caseId":"carly"}'
  # then check: browser DevTools Network tab while sending a chat message,
  # and: sqlite3 backend/ptai_dev.db 'SELECT case_id,status FROM interview_sessions;'
  ```

### P2 — Stale `sessionId` can bind a new interview to the previous patient's session · **CRITICAL (latent)**
- **Files:** `src/state/AppContext.tsx` (persists `sessionId`, `messages`, `interviewStartTime` to localStorage), `src/pages/InterviewPage.tsx` (mount effect + session-create effect).
- **Trace:** sessions are case-bound at creation (`POST /api/sessions {caseId}`); turns use only `sessionId`. If the user leaves a Camden interview without finishing the assessment flow, `interviewStartTime` stays truthy → on entering `/interview/carly` the mount effect **skips** `setSessionId(null)` → the session-create effect sees a truthy (Camden) `sessionId` and **skips creating a Carly session** → every Carly message would be answered by the backend *as Camden*. This is the exact backend leakage mechanism the audit asked about; it hasn't fired yet only because no session was ever created (P1).
- **Aggravator:** `/interview/:caseId` reuses the same route element, so React Router does **not** remount `InterviewPage` when only the param changes — the mount effect (empty dependency array) never re-runs on case switch.
- **Root cause category (from your Phase 4 list):** *frontend using stale selected-case state / cached session from a prior session.* Backend mapping, case loader, fact selector, and prompt isolation are correct (see §D and isolation notes below).

### P3 — Stale transcript & the screenshot bug · **HIGH**
- **Files:** `src/state/AppContext.tsx` (localStorage persistence), `src/pages/InterviewPage.tsx` (reset gated on `!interviewStartTime`), `src/pages/AssessmentResultsPage.tsx:43-53` (the **only** `resetInterview()` call sites).
- **Evidence:** the Camden line in the Carly screenshot = `mockResponses.camden[0]`.
- **Root cause:** interview state is reset only when the user clicks "Try Another Case" / "Return Home" on the results page. Any other navigation path (back button, header logo, direct case selection) carries the previous patient's transcript, timer, and session into the next interview.

### P4 — Timer shows 514:23 · **HIGH**
- **Files:** `src/state/AppContext.tsx` (persisted `interviewStartTime`), `src/pages/InterviewPage.tsx` (reset condition), `src/components/interview/InterviewTimer.tsx:8-14`.
- **Exact cause:** (1) `interviewStartTime` (epoch ms — units are correct, no s/ms confusion) is restored from localStorage from a session started hours earlier and never reset (P3); (2) `formatElapsed()` renders `MM:SS` with unbounded minutes and no hour segment, so ~8 h 34 m displays as `514:23`. Interval creation/cleanup is correct (single interval, cleared on unmount); no timezone involvement.

### P5 — Voice interaction does not exist · **HIGH (missing feature, not a regression)**
- **Files:** `src/components/interview/MicrophoneButton.tsx` (tooltip-only placebo: "Voice support will be connected in a later phase"), `src/components/interview/ConversationPanel.tsx` (renders it).
- **Evidence:** repo-wide grep for `SpeechRecognition|webkitSpeech|speechSynthesis|SpeechSynthesisUtterance|getUserMedia|MediaRecorder` → zero matches in `src/`.
- There is no speech state machine (IDLE/LISTENING/PROCESSING/SPEAKING/…), no permission request, no transcript insertion, no TTS, and therefore no mic/speaker mutual exclusion to audit.

### P6 — Backend fallback replies break character (third person) · **MEDIUM**
- **File:** `backend/app/patient_engine/fallback_manager.py` (`build_fallback`).
- **Evidence (executed simulation):** Carly + "Hi" → fallback would reply **"Her name is Carly Wishard (maiden name Coleman), she is 38 years old."** — a third-person fact dump as a patient utterance.
- **Root causes:** (1) fact texts are intentionally stored third-person for prompt grounding, but the fallback emits `fact.text` verbatim; (2) the natural-greeting branch is only reachable when `not on_topic` — every case has a `greeting`-topic identity fact, so the branch is effectively dead; (3) `pool[0]` is deterministic → repeated identical answers ignore the actual question.

### P7 — Topic classifier gaps make answers feel non-contextual · **MEDIUM**
- **File:** `backend/app/patient_engine/topic_classifier.py`.
- **Evidence (executed):** `"What brought you in today?"` → `['other']`; `"Tell me about your wrists"` → `['other']` (no body-part keywords: wrist/elbow/ankle/hand/joint); `"How does that affect your daily life?"` → `['other']` (no topic memory for pronoun follow-ups). Greetings classify correctly (`"Hi"`, `"How are you?"` → `greeting`).
- **Impact:** on the OpenAI path this is partially compensated (recent history *is* sent; open facts still included), but probe-level facts for the intended topic are withheld; on the fallback path the reply degenerates to the diagnosis line. There is no "current topic" tracking between turns (`context_resolver` carries messages, not topics).

### P8 — Prompt lacks an explicit social/greeting instruction · **LOW**
- **File:** `backend/app/prompts/patient_developer_prompt.txt`. Rules cover grounding, brevity, and character integrity, but nothing says "answer greetings and small talk naturally before clinical content." Combined with P7 this is why greeting turns risk symptom-flavored replies once the backend is actually in the loop.

### P9 — No per-turn observability · **LOW**
- **Files:** `backend/app/patient_engine/__init__.py`, `app/services/interview_service.py`, `app/core/logging.py`.
- Only failure-path warnings exist. Nothing logs `session_id / case_id / turn_number / detected_topic / openai_called / fallback_used / validation_status`, which is exactly what would have made P1 obvious on day one. (No API keys are logged anywhere — checked.)

### P10 — Endpoint naming differs from the audit's expected spec · **INFO**
The implemented API has no `/v1` and different turn routes. Nothing is broken — frontend and backend agree with each other — but tests/docs written against the `/api/v1` spec will 404. See §C.

**Case isolation audit (Phase 4/6 data checks) — clean at the data/prompt layer:**
all four case files load, IDs (`camden`, `carly`, `sofia`, `jayden`) are unique, lowercase,
and exactly match frontend IDs and portrait filenames; `case_loader` is keyed by ID with no
aliases; no fact text or persona in any case file references another patient's name
(re-verified programmatically today); `fact_selector`/`prompt_builder` operate on the single
loaded `CaseDefinition`; `response_validator` explicitly rejects replies containing another
case's patient name; the per-case fallback pool draws only from the case's own facts. The
`lru_cache` in `case_loader` caches a `dict` of Pydantic models — shared, but never mutated
by request code. **The leakage risk is entirely in frontend state lifecycle (P2/P3), not in
the backend engine.**

---

## C. API connection map

Implemented and traced (all under prefix `/api`, base URL `VITE_API_BASE_URL` → default `http://localhost:8000`):

| Frontend component | Frontend service (`src/services/api.ts`) | Endpoint | Backend route | Backend service | Persists / calls |
|---|---|---|---|---|---|
| `CaseSelectionPage` / `usePatientCase` | `fetchCases()` | `GET /api/cases` | `api/cases.py:list_cases` | `case_service.list_cases` | `app/cases/*.json` |
| (available, unused by UI) | `fetchCase(id)` | `GET /api/cases/{id}` | `api/cases.py:get_case` | `case_service.get_case` | case JSON |
| `InterviewPage` (mount) | `createSession()` | `POST /api/sessions` | `api/sessions.py:create_session` | `session_service.create_session` | `students`, `interview_sessions` |
| `InterviewPage.handleSendMessage` | `sendStudentMessage()` | `POST /api/interviews/{sessionId}/messages` | `api/interviews.py:send_message` | `interview_service` → patient engine | `conversation_turns` ×2, **OpenAI** |
| (available, unused by UI) | `fetchSession()` | `GET /api/sessions/{id}` | `api/sessions.py:get_session` | `session_service.get_session` | reads transcript |
| `InterviewPage.handleConfirmEnd` | `completeSession()` | `POST /api/sessions/{id}/complete` | `api/sessions.py:complete_session` | `session_service.complete_session` | locks session |
| (nothing calls it) | — | `GET /api/health` | `api/health.py` | — | `SELECT 1` |

Request/response schemas match end-to-end (camelCase aliases on the backend: `studentName`,
`studentId`, `caseId` → `SessionCreateRequest`; responses expose `sessionId`, `patientMessage.text`,
etc. — confirmed against `src/services/api.ts` types; `tsc` passes).

Expected-vs-actual for the audit's endpoint list:

| Audit expected | Actual implemented | Status |
|---|---|---|
| `GET /api/v1/health` | `GET /api/health` | exists, no `/v1` |
| `GET /api/v1/cases` | `GET /api/cases` | exists, no `/v1` |
| `GET /api/v1/cases/{id}/introduction` | `GET /api/cases/{id}` (introduction fields inlined) | different shape, same data |
| `POST /api/v1/sessions` | `POST /api/sessions` | exists, no `/v1` |
| `POST /api/v1/sessions/{id}/turns` | `POST /api/interviews/{id}/messages` | different path |
| `GET /api/v1/sessions/{id}/turns` | `GET /api/sessions/{id}` (messages embedded) | different path |
| `POST /api/v1/sessions/{id}/complete` | `POST /api/sessions/{id}/complete` | exists, no `/v1` |

Common connection problems checklist: wrong base URL — possible **[verify locally]**; missing
`/api/v1` — N/A (consistent both sides); wrong port — possible **[verify locally]**; backend not
running — most likely; CORS — **real risk via Vite port drift (5174 not allow-listed)**; mixed
HTTP/HTTPS — no (both http); env var — root `.env.local` absent, default used; route paths &
body/response fields — match; session ID not saved — saved, *over*-persisted (P2); case ID not
passed — passed at session creation; React reading hardcoded case data — **yes, as designed
fallback** (`services/cases.ts` → `mockCases`); frontend silently catching API errors — **yes (P1)**.

---

## D. Patient-response trace — one Carly message

**Intended path** for `“How are you feeling today?”` in a Carly session:

```
InterviewPage.handleSendMessage("How are you feeling today?")
→ api.sendStudentMessage(sessionId, text)
→ POST /api/interviews/{sessionId}/messages
→ interview_service.send_student_message
   ├─ SessionRepository.get(sessionId)            → session.case_id = "carly", locked = False
   ├─ TranscriptRepository.append_turn(student)   → turn_index N
   ├─ patient_engine.generate_patient_response("carly", …)
   │   ├─ topic_classifier.classify → ["greeting", "emotional_wellbeing"]   (executed, confirmed)
   │   ├─ context_resolver → last ≤12 turns as chat history
   │   ├─ fact_selector("carly", topics) + core "condition" facts
   │   ├─ DisclosureManager → open facts + probe/sensitive only for matched topics, ≤5 new
   │   ├─ prompt_builder → developer prompt: Carly persona + selected Carly facts only
   │   ├─ OpenAIPatientClient.generate (gpt-4o-mini, key present in backend/.env)
   │   └─ response_validator → rejects character breaks & any other patient's name
   ├─ TranscriptRepository.append_turn(patient)   → turn_index N+1
   └─ commit → response {studentMessage, patientMessage}
→ frontend deliverPatientMessage(exchange.patientMessage.text)
```

**Actual path today** (evidence: empty database):

```
handleSendMessage → sessionId is null (createSession failed silently at mount)
→ window.setTimeout(1200 ms)
→ getMockResponse("carly", turnIndex)   // src/data/mockResponses.ts — 6 canned lines, cycled
→ canned line rendered; nothing sent anywhere; nothing saved
   + any leftover messages from the previous (Camden) interview are still displayed (P3)
```

---

## E. Voice-system report

**Status: missing entirely** (not partially implemented).

| Capability | State | Files |
|---|---|---|
| Speech input (Web Speech API, permissions, start/stop/onresult/onerror/onend, transcript insertion, auto-submit) | Missing | would live in a new `src/services/speech.ts` + `MicrophoneButton.tsx` |
| Speech output (`speechSynthesis`, utterance, voice selection, cancel-prior) | Missing | new service + `InterviewPage.tsx` |
| Speech state machine (IDLE/LISTENING/PROCESSING/SPEAKING/COOLDOWN/ERROR/FINISHED) | Missing — current `InterviewStatus` has `ready/listening/processing/patient-speaking` (`src/types/interview.ts`), a usable seed | `types/interview.ts`, `InterviewPage.tsx` |
| Mic-never-listens-while-patient-speaks rule | Nothing to enforce it yet | — |
| Current mic button | Visual placebo: onClick shows a 2.5 s tooltip | `src/components/interview/MicrophoneButton.tsx` |

Browser limitations to plan for: `SpeechRecognition` is Chrome/Edge (`webkitSpeechRecognition`)
and partially Safari; **Firefox has no SpeechRecognition** — a clear unsupported-browser message
is required. `speechSynthesis` is broadly supported but voice inventory varies per OS/browser;
prior utterances must be `cancel()`ed before speaking new ones. Echo risk (mic hearing TTS) makes
the mutual-exclusion + short cooldown rule mandatory.

---

## F. Timer report

**Exact cause, two parts:** (1) `interviewStartTime` is persisted to localStorage
(`AppContext`) and is reset only by `resetInterview()` on the Assessment Results page or when
entering an interview with `interviewStartTime === null`; abandoning an interview and starting
another restores a start time hours old. (2) `InterviewTimer.formatElapsed()` formats
`MM:SS` with unbounded minutes — 30,863 s → `514:23`. Units (ms), interval lifecycle, and
cleanup are all correct; no duplicate intervals; no timezone math involved.

---

## G. Recommended repair order (by dependency)

1. **Make connection failures visible** — log every API error to console, add a small UI
   indicator ("live patient" vs "offline demo"), and temporarily disable the mock fallback
   during debugging. Then run the P1 curl checklist and fix whichever trigger appears
   (start order, pin Vite to port 5173 via `server.port` or add 5174 to `CORS_ORIGINS`,
   set `VITE_API_BASE_URL` in `.env.local`). Success criterion: rows appear in
   `interview_sessions` / `conversation_turns`.
2. **Fix interview state lifecycle (kills P2, P3, and most of P4)** — on entering
   `/interview/:caseId`: always clear `messages`, `interviewStartTime`, `sessionId` when the
   case differs from the last session's case (store `caseId` beside `sessionId`); make the
   effect react to `caseId` (or `key` the page by `caseId`); reset state on interview entry
   rather than only on results-page exit.
3. **Timer formatting** — add hour rollover (`H:MM:SS`) as defense in depth.
4. **Backend fallback + classifier quality (P6, P7)** — first-person fallback lines, working
   greeting branch, body-part and presenting-concern keywords, simple last-topic memory for
   pronoun follow-ups.
5. **Prompt greeting rule (P8)** — one added instruction line.
6. **Observability (P9)** — per-turn structured log: `session_id, case_id, turn, topic, openai_called, fallback_used, valid`.
7. **Voice (P5, new feature)** — speech service with the state machine, mic/TTS mutual
   exclusion, cooldown, Firefox fallback message.
8. Optional: align routes to `/api/v1/...` or update external specs/tests to the actual paths.

## H. Files that require modification (when fixes are approved — none touched yet)

Frontend: `src/pages/InterviewPage.tsx`, `src/state/AppContext.tsx`,
`src/components/interview/InterviewTimer.tsx`, `src/services/api.ts`, `src/services/cases.ts`,
`src/components/interview/MicrophoneButton.tsx`, `src/components/interview/ConversationPanel.tsx`,
`src/types/interview.ts`, `vite.config.ts`, new `src/services/speech.ts`, new `.env.local`.

Backend: `app/patient_engine/fallback_manager.py`, `app/patient_engine/topic_classifier.py`,
`app/patient_engine/context_resolver.py` (topic memory), `app/prompts/patient_developer_prompt.txt`,
`app/patient_engine/__init__.py` + `app/services/interview_service.py` (logging),
optionally `app/main.py`/routers (`/v1` prefix), `backend/.env` (CORS origins if port drifts).
