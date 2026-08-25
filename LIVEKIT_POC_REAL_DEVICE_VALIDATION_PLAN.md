# LiveKit POC — Real-Device Validation Plan

Phase 1 (code) is complete: 460 backend tests pass, 87 frontend tests pass,
`tsc -b` and `npm run build` are clean, nothing is committed. This document
does **not** change architecture. It prepares the existing Phase 1 code for
real LiveKit Cloud testing and defines how to validate it.

No blockers were found while preparing this plan. Two small, additive,
Phase-1-only fixes were made first (see "What changed just now" below) because
they were required to actually measure the latency this validation cares
about — nothing about `/interview`, `/api/interviews/*`, or `/api/voice/*`
was touched.

---

## 0. What changed just now (prep, not new architecture)

1. **Timing instrumentation added** (was completely absent before):
   `backend/app/livekit_agent/patient_adapter.py` and `worker.py` now record
   a per-turn stage breakdown and log it as **one line**:
   `livekit_poc_turn_timing client_turn_id=<id> openai_slot_wait_start=+Xms
   openai_slot_acquired=+Xms openai_response_complete=+Xms persisted=+Xms
   tts_slot_wait_start=+Xms tts_slot_acquired=+Xms tts_response_complete=+Xms
   first_audio_publish_start=+Xms speech_complete=+Xms`. Never logs patient
   text, audio, or secrets — stage names and elapsed milliseconds only.
2. **Known, honest limitation**: `openai_slot_acquired` ≈ `openai_request_start`
   and `tts_slot_acquired` ≈ `tts_request_start` are the same instant in these
   logs. Separating them further requires instrumenting `app/patient_engine/`
   or the ElevenLabs client itself, which are production files — out of scope
   right now. Treat the two pairs as merged.
3. **Frontend now sends `durationMs`** on `livekit_patient_audio_started`
   (time from student turn sent → first audio signal) and
   `livekit_patient_audio_completed` (total turn time) telemetry events. This
   field existed in the backend schema already but the frontend never
   populated it for *any* event — now fixed for the LiveKit events.
4. **Agent log lines are now timestamped** (`worker.py`'s `logging.basicConfig`
   previously had no timestamp format, making cross-process correlation
   impossible).
5. **The POC page now displays the real room name** returned by the token
   endpoint (`Room: ptai-poc-<sessionId>`), instead of requiring you to
   hand-type/concatenate it — reduces tester error when copying it into the
   agent worker's `--room` flag.

All five changes are confined to `backend/app/livekit_agent/`,
`src/services/livekit/`, `src/services/voiceDiagnostics.ts` (additive field
only), and `src/pages/admin/system/LiveKitTestPage.tsx`. Full suites re-run
clean after each change (460 backend / 87 frontend). Nothing committed.

---

## 1. LiveKit Cloud setup

1. Go to your LiveKit Cloud dashboard (cloud.livekit.io) and sign in / create
   an account.
2. Create a new **Project** (e.g. name it `ptai-poc`). Do not enable
   SIP/Egress/Ingress — Phase 1 doesn't use them and they add cost surface.
3. In the project's **Settings → Keys** page, generate (or use the
   auto-created) API Key/Secret pair. Copy both — the secret is shown only
   once.
4. Note the **WebSocket URL** shown on the project dashboard/settings page —
   it looks like `wss://<your-project>-<random>.livekit.cloud`. This is
   `LIVEKIT_URL`.
5. Check your plan's current free-tier connection-minute limits on the
   dashboard before running extended multi-turn/multi-device sessions — don't
   assume a number here, LiveKit's pricing terms can change.

### Environment variables

Add these to `backend/.env` (already gitignored — **never** commit this file,
never paste the secret into chat, a commit message, or this repo):

```
LIVEKIT_URL=wss://<your-project>.livekit.cloud
LIVEKIT_API_KEY=<from LiveKit Cloud dashboard>
LIVEKIT_API_SECRET=<from LiveKit Cloud dashboard>
LIVEKIT_POC_ENABLED=true
```

Restart the backend after editing `.env` (settings are read at process
start). Confirm it took effect: `POST /api/livekit/token` should stop
returning `503 livekit_not_configured` once you're logged in as admin with an
owned session id (see §3).

---

## 2. Exact commands

Run these in **three separate terminals**, all from the repo root
(`/Users/bobbydhir/Desktop/PT-AI-Patient-Dual-Assessment`) unless noted.

### Terminal 1 — backend

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000 2>&1 | tee /tmp/ptai_backend.log
```

### Terminal 2 — LiveKit agent worker

Don't start this until you have a room name + session id from the POC page
(§3) — the `--room`/`--session-id` values are per-session, not fixed.

```bash
cd backend
source .venv/bin/activate
python -m app.livekit_agent.worker \
  --room <paste room name from the POC page> \
  --session-id <paste session id from the POC page> \
  --case-id carly 2>&1 | tee /tmp/ptai_agent.log
```

It will exit immediately with a clear message if `LIVEKIT_URL` /
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` aren't set — that's the fail-closed
behavior working as designed, not a bug.

### Terminal 3 — frontend

```bash
npm run dev
```

Opens on `http://localhost:5173` by default.

### Admin login

You need an **admin** account (`/admin/system/livekit-poc` is gated by
`ProtectedRoute role="admin"` and the backend's `require_admin`). If you don't
already have one:

```bash
cd backend
source .venv/bin/activate
ADMIN_EMAIL='you@example.edu' ADMIN_PASSWORD='choose-a-strong-password' \
  python -m scripts.create_admin
```

---

## 3. Starting a POC interview session — how session ID and room name actually work

This matches the implementation exactly (`src/pages/admin/system/LiveKitTestPage.tsx`,
`src/services/livekit/livekitPocEngine.ts`, `backend/app/api/livekit.py`,
`backend/app/services/livekit_token_service.py`):

1. Log in as the admin account at `http://localhost:5173`, then navigate to
   `http://localhost:5173/admin/system/livekit-poc`.
2. Click **Start Interview** once. This does three things automatically, in
   order:
   - `POST /api/sessions` creates a **brand-new** `InterviewSession` owned by
     your admin account, case `carly` (never a real student's session).
   - `POST /api/livekit/token` mints a room-scoped token; the room name is
     **always** server-derived as `ptai-poc-<session_id>` — there is no way
     for the client to choose a different room name (the request schema has
     no such field).
   - The browser joins the LiveKit room and publishes the microphone.
3. The page now shows **Session: `<uuid>`** and **Room:
   `ptai-poc-<uuid>`** — copy the room name and session id directly from the
   page into Terminal 2's command (§2). Do not hand-construct the room name
   yourself; use exactly what's displayed.
4. Start the agent worker (Terminal 2) with those two values.
5. Once the worker connects, the page's diagnostics panel should show **Room
   connected → Microphone published → Agent connected → Patient audio track
   subscribed** (order may vary slightly — Agent connected and Patient track
   subscribed both depend on the worker actually running).
6. Speak. The recognized text is sent to the agent over a LiveKit data
   message; the agent calls the same `generate_patient_response` /
   ElevenLabs pipeline production uses, and publishes audio back on a single
   persistent WebRTC track.

A fresh session/room is created **every time you click Start Interview** —
there's no way to reuse a room across page reloads. If you refresh the page,
you must also restart (or point to a new room for) the agent worker.

---

## 4. Real-device validation checklist

Use one copy of this checklist **per device**. Do not mark Phase 1 successful
from automated tests alone — this checklist is the actual acceptance gate.

**Devices to cover:** iPhone Safari, iPhone Chrome, Android Chrome, desktop
Chrome, desktop Firefox.

### 4.1 Per-device setup
- [ ] Device is on the same network as the dev machine (or `VITE_API_BASE_URL`
      / CORS are adjusted for LAN access — the default `CORS_ORIGINS` only
      allows `localhost`; you likely need to add your LAN IP:port to
      `CORS_ORIGINS` in `backend/.env` and browse to
      `http://<dev-machine-LAN-IP>:5173` from the phone).
- [ ] Backend, agent worker, and frontend are all running (§2).
- [ ] Logged in as admin on the device's browser.
- [ ] Opened `/admin/system/livekit-poc`, clicked Start Interview, agent
      worker started with the shown room/session id.

### 4.2 Core 10–20 consecutive turns
Speak 10–20 short clinical-interview-style questions in a row, one at a time,
waiting for each patient reply before the next. For **every** turn record
pass/fail:

- [ ] Microphone remains usable for all turns (no permission re-prompt, no
      dead mic).
- [ ] LiveKit connection stays `connected` the whole session (no unexpected
      `Reconnecting`).
- [ ] Patient audio track stays subscribed (diagnostics panel stays green,
      does not need to be re-triggered).
- [ ] ElevenLabs voice plays **automatically** — no tap/click required to
      hear each reply.
- [ ] Zero taps required beyond the single initial Start Interview.
- [ ] No robotic `speechSynthesis` voice at any point (this path has no
      fallback by design — if you ever hear the robotic voice on this page,
      something is badly wrong; the correct failure mode is an explicit error
      on screen, not the robotic voice).
- [ ] No page refresh required at any point.
- [ ] State machine visibly follows LISTENING → THINKING → SPEAKING →
      LISTENING correctly for every turn (watch the "Current state" badge).
- [ ] No duplicate turns (patient never answers the same question twice).
- [ ] No missing turns (every question gets exactly one answer).
- [ ] No stale-turn playback (an old answer never plays after a newer
      question was asked).
- [ ] Turn count displayed on the page matches your actual spoken-question
      count.

**Result for this device:** ☐ PASS (≥10 consecutive clean turns) ☐ FAIL (note
which turn # and which checkbox above failed)

### 4.3 Failure/recovery scenarios (run once per device, after the core test)

- [ ] **Wi-Fi disconnect/reconnect mid-call**: turn Wi-Fi off for ~5s during a
      patient reply, turn it back on. Expected: `Reconnecting` state shown,
      then either recovery to `listening` or an explicit `error` state — never
      a silent hang, never robotic fallback.
- [ ] **Temporary network degradation**: throttle to a slow profile (e.g.
      browser devtools "Slow 3G" on desktop, or step a few rooms away from
      the router on mobile) for one turn. Expected: turn either completes
      (slower) or the THINKING watchdog times out into an explicit error —
      never a hang past ~20s.
- [ ] **Background → foreground (mobile only)**: background the browser tab/
      app mid-turn (press home button / switch app), wait 10s, foreground it
      again. Expected: either the turn completes normally or an explicit
      error/reconnect is shown — check whether audio was missed silently.
- [ ] **Microphone interruption**: on mobile, receive a phone call (or
      simulate one) mid-listening. Expected: mic access is cleanly
      reacquired or an explicit error is shown, never a silently-dead mic
      with no on-screen indication.
- [ ] **End Interview during patient speech**: click End Interview while
      audio is actively playing. Expected: audio stops immediately, no
      further turns processed, page returns to a clean idle-like state, no
      console errors.
- [ ] **Temporary TTS failure**: (needs a deliberate trigger — e.g.
      temporarily set `ELEVENLABS_API_KEY` to an invalid value and restart
      the backend+worker for this one test, then restore it). Expected: the
      turn surfaces an explicit on-screen error — **never** a silent
      fallback to browser `speechSynthesis`.

**Any silent fallback to browser TTS observed?** ☐ Yes (STOP — this is a
correctness bug, report immediately, do not continue rollout) ☐ No

---

## 5. Reading the timing data

After each device session, pull the per-turn latency breakdown:

```bash
grep livekit_poc_turn_timing /tmp/ptai_agent.log
```

Each line gives the backend-side breakdown for one turn (all values are
milliseconds **since the agent received the student's text**):
`openai_slot_wait_start`, `openai_slot_acquired` (≈ OpenAI request start),
`openai_response_complete`, `persisted`, `tts_slot_wait_start`,
`tts_slot_acquired` (≈ TTS request start), `tts_response_complete`,
`first_audio_publish_start`, `speech_complete`.

For the frontend's end-to-end number (student-perceived latency), open the
browser devtools console during testing — `livekit_patient_audio_started` and
`livekit_patient_audio_completed` log lines (visible at `warn`/`debug` level,
see `voiceDiagnostics.ts`) now carry a `durationMs` field: time-to-first-audio
and total-turn-time respectively. These also ship to the backend via
`POST /api/voice/telemetry` and appear in the backend log as
`voice_telemetry event=livekit_patient_audio_started ... duration_ms=<N>`
lines in `/tmp/ptai_backend.log`.

Reminder: `tts_response_complete` is when the **entire** ElevenLabs response
has been buffered (this POC's `synthesize_patient_audio_pcm` does not stream
partial audio to WebRTC as chunks arrive — it waits for the full PCM payload
before publishing any frame). This is an architecturally honest Phase-1
limitation, not a bug: `first_audio_publish_start` will not fire meaningfully
earlier than `tts_response_complete`. If TTS-to-first-audio latency turns out
to be a problem in real-device testing, that is a Phase 2 discussion
(streaming TTS→WebRTC), not something to silently patch now.

---

## 6. Concurrency validation ladder (plan only — do not execute yet)

Only proceed here after single-device (iPhone Safari in particular) is
**stable** per §4's acceptance criterion. Ramp in this order — do not jump
ahead:

**1 device → 3 → 5 → 10 → 20** (do **not** go to 65–70 yet).

At each level, run each device through the same §4.2 core-turns test
*simultaneously*, and for the batch as a whole capture:

- LiveKit/WebRTC connection latency (room join → first `Connected` event) —
  from browser console `livekit_room_connecting`→`livekit_room_connected`
  timestamps.
- OpenAI queue wait — `openai_slot_wait_start` → `openai_slot_acquired` in
  each agent's timing line; also cross-check against the existing System
  Dashboard / Traffic page (`/admin/system/traffic`) which already reports
  fleet-wide `interview_capacity()` (active/limit/waiting/p50/p95) — this is
  the SAME semaphore the LiveKit agents are drawing from, so concurrent
  legacy-path traffic during a test will show up there too.
- OpenAI generation latency — `openai_slot_acquired` → `openai_response_complete`.
- ElevenLabs queue wait — `tts_slot_wait_start` → `tts_slot_acquired`.
- ElevenLabs TTS latency — `tts_slot_acquired` → `tts_response_complete`.
- Total speech-to-first-patient-audio latency — frontend `durationMs` on
  `livekit_patient_audio_started` (§5).
- Failures/reconnections — count of `livekit_room_reconnecting`/`error`
  states across all devices in the batch.
- Dropped turns — count of `livekit_poc_agent_turn_dropped_busy` lines in the
  agent log (one agent process per session, so this should be rare/absent
  unless a single student spoke over their own pending turn).
- TTS capacity exhaustion — count of `livekit_poc_tts_no_capacity` lines
  (means `MAX_CONCURRENT_TTS_REQUESTS` was fully consumed **fleet-wide**,
  same cap the legacy path shares).

Remember: **one agent worker process per concurrent POC session** is required
at this stage (Phase 1 has no agent-dispatch/pooling — that's explicitly a
later-phase concern). Running 20 simultaneous POC sessions means 20 terminal
windows each running `python -m app.livekit_agent.worker` pointed at a
different room. This is expected and fine for a concurrency *validation*, not
a production design — do not read this limitation as something to fix right
now.

---

## 7. Acceptance criterion (do not skip)

Phase 1 is **not** considered validated until you have real-device evidence —
not just passing automated tests — that:

> iPhone Safari can sustain at least 10–20 consecutive turns with no robotic
> fallback, no refresh, no repeated taps, no duplicate audio, and no broken
> turn state (§4.2, §4.3's "silent fallback" check in particular).

Automated tests (460 backend / 87 frontend, all passing) prove the code is
internally correct and the safety-critical semaphore reuse holds. They do
**not** prove a real iPhone Safari WebRTC session behaves this way — only
this checklist does.
