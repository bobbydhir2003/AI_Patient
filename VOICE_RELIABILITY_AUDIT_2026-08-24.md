# PT AI Patient — ElevenLabs Voice Reliability Audit (2026-08-24)

**Type:** Read-only source audit. No code was modified during this audit.
**Supersedes on one critical point:** the existing `VOICE_RELIABILITY_SCALABILITY_AUDIT.md` (2026-08-21) claimed the sentence-queue reducer already isolates per-sentence TTS failures. **That claim was wrong for the code actually committed at `HEAD`.** This audit re-verified the reducer directly against `HEAD` and found the opposite. See §3.
**Working-tree note:** `git status` shows **uncommitted, undeployed** changes to `src/services/patientStreamService.ts`, `src/services/patientVoiceService.ts`, `src/services/streamingQueueState.ts`, plus a new untracked `src/services/voiceDiagnostics.ts`. These changes already fix the bug in §3. Everything below is reported for **both** states — "HEAD (deployed today)" and "working tree (drafted, not deployed)" — because what's live in production is what's actually causing the symptom you're seeing.

---

# 1. Executive Summary

Two independent things are wrong, and they stack:

**A) A real code bug, confirmed at `HEAD` (what's deployed today).** The sentence-audio state machine in `src/services/streamingQueueState.ts` has this reducer case, exactly as committed:

```ts
case "AUDIO_FAILED": {
  const s = state.sentences.find((x) => x.index === event.index);
  if (!s || (s.status !== "fetching" && s.status !== "queued")) return state;
  return { ...withSentence(state, event.index, "failed"), voiceFailed: true };
}
```

**One sentence's TTS hiccup sets a whole-turn flag (`voiceFailed = true`).** Every later sentence's `pendingFetches` selector excludes anything once `voiceFailed` is true, so **zero further ElevenLabs requests are even attempted** for the rest of that reply — every remaining sentence falls straight to the robotic browser voice. This is *precisely* the failure mode you described in Part 2 of your request ("sentence 2 fails → sentence 3+ disabled → browser voice"), and it is sitting in the code exactly as written, not a hypothesis.

`voiceFailed` is **per-turn (in-memory, closed over inside `startStreamingExchange`)**, not session- or globally-scoped — a new turn starts a fresh `initialStreamQueueState` with `voiceFailed: false`. That is why the *next* turn (or a refresh, which restarts everything) usually recovers: it's not that ElevenLabs "came back," it's that a fresh turn gets a fresh, unpoisoned flag. This exactly explains "sometimes it stops, sometimes refreshing fixes it."

**B) A second, independent bug that fires before any sentence exists.** The up-front voice-availability probe (`getVoiceStatus`, `patientStreamService.ts:123-137` and `patientVoiceService.ts:114-130`) calls `GET /api/voice/status/{case}` once per turn. If *that* one lightweight request errors or throws (network blip, slow backend, whatever), the `catch` block returns `{ available: false }` and — **this failure is never cached** — `voiceFailed` is set to `true` for the *entire upcoming turn before a single sentence is even generated* (`patientStreamService.ts:596-599`). Verified independently in this audit: `voice_status` (`backend/app/api/voice.py:47-61`) **never calls ElevenLabs at all** — it's a local case-config lookup behind `get_current_user` (DB-backed auth). So a failure here is *never* an ElevenLabs problem; it's app/DB/network latency on your own box masquerading as "ElevenLabs is down."

**Why only 3 students already triggers this, and why it's intermittent:** neither bug requires load. Bug A needs exactly one transient sentence-level hiccup (a slow ElevenLabs first byte, a Wi-Fi/cell blip on a phone, a momentary Redis timeout denying a slot) anywhere in a multi-sentence reply. Bug B needs exactly one slow/failed auth-DB round trip. Both are far more likely on a mobile connection (higher packet loss, connection handoffs between cell towers, backgrounding) than on a laptop — which matches your observation that mobile is worse. **Neither bug's probability scales with ElevenLabs concurrency; both scale with "did any one small thing hiccup during this turn," which is a roughly constant per-turn probability regardless of whether 3 or 30 students are active.** That is why 3 students already show it and why simply reducing load would not fully fix it.

**A fix for A is already drafted in your working tree, uncommitted.** It changes `AUDIO_FAILED` to mark only that one sentence and leaves `voiceFailed` untouched, adds a bounded one-retry for transient per-sentence failures, adds a Blob-download recovery step before giving up to browser voice on the atomic path, and adds classified client-side diagnostic logging (`voiceDiagnostics.ts`, console-only today, not yet shipped to a server). **Bug B (the probe) is untouched by the current draft — it still needs a fix.**

**Is ElevenLabs the bottleneck at 3 users? No — with one caveat that needs an account-side check.** The app is architecturally sequential per student (no `Promise.all`/parallel fan-out anywhere in the voice path — confirmed by reading both pumps), so 3 students generate at most 3 concurrent ElevenLabs HTTP calls, against an app-configured cap of 15 (code default) or 10 (`.env.example`). That is nowhere near a self-imposed ceiling. **But the app's cap is a number someone picked, not ElevenLabs' actual account limit** — and this audit could not authoritatively confirm your Creator plan's real concurrent-request ceiling (ElevenLabs' help pages block automated fetches; third-party sources disagree, ranging ~5–15 depending on product/model). If your account's true limit is on the low end of that range, it is *plausible* — not confirmed — that concurrent testers/tabs/staff using the same API key could occasionally trip ElevenLabs' own 429. This needs one dashboard check (see §6, §13).

---

# 2. Architecture Map

```
Student (mobile/desktop browser)
   │
   ├─ Typed OR voice-mode gesture (unlockAudioPlayback() fires here — iOS gesture chain)
   │      src/hooks/useVoiceConversation.ts:365,404  |  src/pages/InterviewPage.tsx:462
   │
   ▼
InterviewPage.performExchange()                        src/pages/InterviewPage.tsx
   → streaming enabled? (OPENAI_PATIENT_STREAMING_ENABLED, default FALSE in code)
        backend/app/core/config.py:94
   ├─ YES → startStreamingExchange()                    src/services/patientStreamService.ts:139
   │         POST /api/interviews/{sid}/messages/stream  (SSE, one request per turn)
   └─ NO  → performAtomicExchange() → speakPatientResponse()  src/services/patientVoiceService.ts:566
             one JSON turn, ONE /synthesize call for the whole reply
   ▼
NGINX (reverse proxy) — config NOT in this repo, no nginx.conf found anywhere
   ▼
UVICORN — exactly 1 worker in production today
   backend/scripts/start.sh:22 → `uvicorn app.main:app --host 0.0.0.0 --port ...`
   (no --workers flag; Docker CMD runs this directly; no systemd unit, no
   docker-compose.yml, no Procfile exists in the repo)
   ▼
POST /messages/stream → interview_stream_service.stream_student_message()
   • reserve openai_interview slot (DistributedSemaphore, cap 20, wait 2s)  backend/app/core/concurrency.py
   • short DB session for context, closed before the OpenAI call
   • streaming_engine.stream_patient_response()  backend/app/patient_engine/streaming_engine.py:132
       – ONE OpenAI streamed call per turn
       – sentences are extracted incrementally and yielded as they complete
        (StreamSentence, streaming_engine.py:247,270) — NOT pre-split, NOT batched
   • commit ONE authoritative turn afterward
   ▼  SSE: event:speech → event:sentence (×N) → event:final
BROWSER — two independent pumps (patientStreamService.ts):
   fetchPump (SEQUENTIAL — one sentence's fetch at a time, awaited fully)  :221
      POST /api/voice/synthesize  {caseId, text, sessionId, turnId:"", speechStyle}
        NGINX ▶ UVICORN ▶ voice.synthesize()             backend/app/api/voice.py:216
           • short DB session: auth + ownership + approved text, closed BEFORE
             the TTS slot is acquired or ElevenLabs is called      voice.py:155-190
           • audio_cache.get() → HIT: return cached bytes, no provider call
           • tts_slot().acquire()  (DistributedSemaphore "tts", cap 15, wait 5s)
               – no slot in 5s → 409 VoiceNotAvailableError → browser voice
           • ElevenLabsClient.stream_speech()  POST api.elevenlabs.io/.../stream
               – model eleven_multilingual_v2 (NOT eleven_flash_v2_5 — that is
                 only a documented future plan, not the configured default)
               – up to 4 physical attempts (1 + 3 retries), INSIDE the held slot
               – retries: 429/5xx/timeout/empty only; NO Retry-After honored
                 (Retry-After IS honored for OpenAI, not for ElevenLabs)
           • StreamingResponse pipes MP3 chunks back; slot released in `finally`
   playPump (STRICTLY ORDERED, no overlap)                :338
      plays sentence blobs via HTMLAudioElement in index order;
      browser TTS (speechSynthesis) for any sentence whose fetch failed
   ▼
Mobile/browser playback (HTMLAudioElement / MediaSource)
   ▼
Fallback voice (window.speechSynthesis) — per-sentence on the streaming path,
per-whole-reply on the atomic path
```

---

# 3. Confirmed Bugs

### Bug 1 — One sentence's TTS failure disables ElevenLabs for the rest of the turn (whole-turn poisoning)

```
STATUS AT HEAD (deployed today): PRESENT — CONFIRMED
STATUS IN WORKING TREE (uncommitted, not deployed): FIXED (drafted, not yet shipped)

FILE: src/services/streamingQueueState.ts
FUNCTION: reduceStreamQueue(), case "AUDIO_FAILED"
LINE (HEAD): ~150-158 in the committed file (git show HEAD:src/services/streamingQueueState.ts)

CODE (HEAD, currently deployed):
  case "AUDIO_FAILED": {
    const s = state.sentences.find((x) => x.index === event.index);
    if (!s || (s.status !== "fetching" && s.status !== "queued")) return state;
    return { ...withSentence(state, event.index, "failed"), voiceFailed: true };
  }

WHAT IT DOES: any single sentence's TTS fetch failure sets state.voiceFailed = true.
pendingFetches(state) (same file) filters out sentences once voiceFailed is true, so
the fetchPump loop in patientStreamService.ts stops issuing new /synthesize calls for
every remaining sentence in that reply, for the rest of that turn.

WHY IT IS A PROBLEM: a single transient event — one slow ElevenLabs first byte, one
mobile network blip, one 409 from a momentarily-full TTS semaphore, one Redis hiccup —
converts a normally-recoverable per-sentence retry situation into "the rest of this
whole reply is robotic," even though ElevenLabs itself is fine and would have served
sentence 3, 4, 5 without issue.

USER-VISIBLE RESULT: exactly what you described — "sentence 1 ElevenLabs, sentence 2
briefly fails, sentence 3+ robotic," self-healing on the next turn (fresh state) or on
refresh (everything resets). This does not require any concurrency pressure — one
hiccup per turn is enough, and hiccup probability does not need 3+ students; it can
happen with exactly 1.

SEVERITY: P0 — CONFIRMED, directly matches the reported symptom, live in production.

STATUS: A fix already exists, uncommitted, in the current working tree:
  case "AUDIO_FAILED": {
    ...
    return withSentence(state, event.index, "failed");   // voiceFailed NOT touched
  }
This is paired with a bounded one-retry in patientStreamService.ts (only for
transient statuses: network error, 408/409/425/429/5xx — permanent 4xx like "text
too long" is not retried) before a sentence is marked failed. This draft has not
been committed or deployed. Verify it with `scripts/test-streaming-queue.mjs`
(also modified, adds regression tests for exactly this bug) before shipping.
```

### Bug 2 — Voice-availability probe treats *any* error as "ElevenLabs unavailable" for the whole upcoming turn

```
STATUS: PRESENT in BOTH HEAD and the working tree — NOT touched by the current draft fix.

FILE: src/services/patientStreamService.ts (mirrored in src/services/patientVoiceService.ts)
FUNCTION: getVoiceStatus() / begin()
LINE: patientStreamService.ts:123-137 (getVoiceStatus), :596-599 (usage)

CODE:
  async function getVoiceStatus(caseId) {
    const cached = voiceStatusCache.get(caseId);
    if (cached) return cached;
    try {
      const status = await fetchVoiceStatus(caseId);
      ... voiceStatusCache.set(caseId, entry); return entry;
    } catch {
      return { available: false, fallbackRate: 0.97 };   // NOT cached
    }
  }
  // begin():
  const status = await getVoiceStatus(options.caseId);
  if (!status.available && !cancelled) {
    state = { ...state, voiceFailed: true };
  }

WHAT IT DOES: before the first sentence of a turn even exists, the frontend calls
GET /api/voice/status/{case}. ANY failure of that call (timeout, 5xx, network drop) —
not just a real "no voice configured" response — is treated identically to a
definitive "this case has no ElevenLabs voice," and sets voiceFailed = true for the
entire turn before a single ElevenLabs request is attempted.

WHY IT IS A PROBLEM: this endpoint, verified in this audit (backend/app/api/voice.py:
47-61), does NOT call ElevenLabs at all — it is a local case-config lookup gated by
get_current_user (a DB-backed auth dependency). Its failures are about your own
backend/DB/network reachability, never about ElevenLabs itself. Treating a probe error
as "voice unavailable" is conflating "my own backend hiccuped for 200ms" with
"ElevenLabs is down," and the cost is the entire turn goes robotic even though
ElevenLabs was never contacted and never failed.

USER-VISIBLE RESULT: an entire reply is spoken by the browser from a single unrelated,
non-ElevenLabs hiccup. Self-heals next turn (failure is deliberately not cached) or on
refresh.

SEVERITY: P0 — CONFIRMED, not yet fixed in either HEAD or the working tree.

RECOMMENDED FIX (not applied): treat a probe *error* as "unknown — attempt ElevenLabs
anyway," and let /synthesize itself (which already degrades per-sentence) be the
source of truth. Only a definitive 200 response with available:false should force
browser voice for the case.
```

### Bug 3 — Client-side voice diagnostics never reach the server

```
FILE: src/services/voiceDiagnostics.ts (new, untracked, uncommitted)
WHAT IT DOES: logs classified events (tts_fetch_failed, tts_audio_play_failed,
tts_progressive_start_timeout, tts_browser_fallback, etc.) to console.warn only.
Also keeps in-memory counters (getVoiceCounters()).
WHY IT IS A PROBLEM: none of this reaches a server. You cannot currently answer
"why did student 2's phone go robotic at 10:32:18" without that student's own
browser console open at that moment.
SEVERITY: P1 — observability gap, not a correctness bug. Blocks diagnosing any of
the above in a real multi-student test (including the one you're planning).
```

---

# 4. Likely Bottlenecks (not confirmed bugs, but real risk)

| # | Bottleneck | Evidence | Why it matters at low N |
|---|---|---|---|
| 1 | Single Uvicorn worker, `t3.micro`-class box, Postgres co-located | `backend/scripts/start.sh:22` (no `--workers`), `docs/DEPLOYMENT.md:12-18` documents current=t3.micro/1 worker/no Redis vs planned=bigger box/4 workers/Redis | A CPU-throttled burstable instance under concurrent TLS streaming increases first-byte latency, which is exactly the kind of transient hiccup that trips Bug 1 and Bug 2. It's not a hard capacity wall at 3 users, it's a probability multiplier on the two bugs above. |
| 2 | Redis fail-closed for the TTS/OpenAI semaphores | `backend/app/core/distributed_semaphore.py:178-193` — `_acquire_redis` returns `None` (deny) whenever `ping_cached()` fails or Redis errors, if `redis_required` (default true in production/staging). 0.5s connect/socket timeouts. | If Redis is provisioned but even briefly slow/unreachable, TTS requests are denied (409 → browser voice) rather than falling back — this is a deliberate, defensible design choice (documented rationale: prevents 4 workers from silently 4x-ing a limit) but it means a flaky/undersized Redis directly causes robotic voice. **Whether Redis is even provisioned in production is unverified from this repo — see §13.** |
| 3 | No nginx.conf in this repo | Confirmed by full-repo search — nothing found. Only prose references (`README.md:524`, `docs/DEPLOYMENT.md:89-90`) saying "unchanged, no config needed." The backend does send `X-Accel-Buffering: no` on the SSE endpoint (`backend/app/api/interviews.py`), hinting nginx not to buffer, but whether nginx actually has `proxy_buffering off` **cannot be verified from the repo.** | If nginx buffers the streamed MP3 (`/api/voice/synthesize`) or the SSE stream, progressive playback delays past the frontend's 10s watchdog (`patientVoiceService.ts:83`) and fails over to browser voice even though both ElevenLabs and the backend were fine. |
| 4 | Retries run inside the held TTS semaphore slot | Confirmed: `elevenlabs_client.py:178-206`, called after `voice.py:247` acquires the slot; released only in `finally` at `voice.py:333-334`. Up to 4 physical attempts, backoff up to 4s each, read timeout 20s each. | Does not amplify *request count against ElevenLabs* in a runaway way (good), but a single slow/erroring ElevenLabs call can occupy one of your 10-15 slots for a worst case of roughly 12s of backoff + up to 4×20s of read timeout ≈ well over a minute in a true pathological case, shrinking effective concurrency for everyone else during that window. |
| 5 | Deferred cancellation on the backend | Verified in this audit by reading the actual installed Starlette/Uvicorn source: `StreamingResponse`'s generator runs via `anyio.to_thread.run_sync(..., abandon_on_cancel=False)`. A client abort (page nav, new message, tab close) does **not** interrupt an in-flight blocking ElevenLabs read; the backend keeps the TTS slot held until that specific read returns or times out (up to 20s), even though the browser has already moved on. | Rapid student interruptions/barge-ins (a designed feature — see `interruptPatient()` in `useVoiceConversation.ts`) or flaky mobile connections that drop and retry frequently can hold slots slightly longer than the "5s wait, 15 slots" model implies. Small at 3 students, compounds at scale. |
| 6 | ElevenLabs account-level concurrency vs. app-configured cap | App cap: `max_concurrent_tts_requests = 15` (code default, `config.py:189`) vs. `10` documented in `.env.example:44` — **the two disagree**, and the live `.env` value is unverified. ElevenLabs' actual Creator-plan concurrent-request ceiling could not be authoritatively confirmed by this audit (see §6). | The app's semaphore is a **local budget guess**, not a mirror of ElevenLabs' real account ceiling. If the true ceiling is lower than 10-15, and anything else shares the same API key concurrently (another tester's tab, an admin voice preview, a second device), ElevenLabs itself can return 429 independent of anything this app's own semaphore tracks. **Needs one dashboard check — see §13.** |

---

# 5. Mobile-Specific Problems

**Audited: `src/services/audioUnlock.ts`, `src/hooks/useVoiceConversation.ts`, `src/pages/InterviewPage.tsx`.**

The iOS/mobile autoplay-unlock implementation is **solid and was NOT a bug in this audit**:

- `unlockAudioPlayback()` (`audioUnlock.ts:59-111`) is called synchronously, before any `await`, from three separate real user-gesture entry points: `startConversation()` (`useVoiceConversation.ts:365`), `retry()` (`useVoiceConversation.ts:404`), and — importantly, this closes a gap the earlier audit flagged as only "likely" fixed — **typed Send** (`InterviewPage.tsx:462`, `handleTypedSend()`), explicitly commented "unlock audio so the patient's spoken reply is allowed to play on iOS Safari even when voice mode was never started." So a student who never taps "Start Voice Conversation" and only types is still covered.
- It resumes a shared `AudioContext`, plays a silent primer through both the Web Audio and `HTMLAudioElement` paths, and primes `speechSynthesis` — all three playback surfaces the app actually uses.
- Backgrounding is handled: `visibilitychange`/`pagehide` tear down mic + audio and move to a resumable `PAUSED` state (`useVoiceConversation.ts:459-477`); the app deliberately never auto-resumes on return (avoids duplicate turns), requiring an explicit tap — which re-arms the unlock via `retry()`/`startConversation()`.

**Why mobile is still worse in practice, despite this:**

1. **Both confirmed bugs (§3) are triggered by transient network hiccups**, and mobile networks (cellular handoffs, Wi-Fi-to-cellular transitions, weaker signal in PT clinic/classroom settings) produce more of exactly that kind of hiccup than a wired laptop connection. Neither bug is mobile-specific code — mobile just has a higher hiccup rate, so it trips the same bugs more often.
2. **`audio.play()` rejection and decode errors are correctly isolated to the failing sentence/turn** (confirmed: `patientStreamService.ts` catches `play()` rejection and falls back to browser TTS for that sentence only in the working-tree version; `patientVoiceService.ts` does the same). This is *not* where the mobile problem comes from — it's already handled per-sentence, not globally.
3. Needs production verification: actual signal quality / connection type of the 3 test devices, and whether ElevenLabs first-byte latency measured from your EC2 region to those devices is materially worse over cellular than over the office/school Wi-Fi used for laptop testing.

---

# 6. ElevenLabs Creator Plan Analysis

**What the code assumes vs. what's actually configured:**

- App-side admission cap: `max_concurrent_tts_requests = 15` (`backend/app/core/config.py:189`), while `backend/.env.example:44` documents `MAX_CONCURRENT_TTS_REQUESTS=10` with the comment "sized for the planned `eleven_flash_v2_5` setup — a tuning knob, not a guaranteed ElevenLabs concurrency limit." **The two disagree, and this repo cannot tell you which value is live in production.**
- Configured model: `eleven_multilingual_v2` (`.env.example:130`) — **not** the faster `eleven_flash_v2_5` that other docs in this repo (and `docs/DEPLOYMENT.md`) already anticipate switching to. This affects latency, not concurrency, but is worth noting since it changes the size of the window during which the frontend's 10s watchdog can fire.
- Real per-student TTS concurrency is sequential (no parallel fan-out anywhere in the frontend voice path — confirmed by reading both `fetchPump` implementations), so **3 active students never generate more than ~3 concurrent ElevenLabs calls, not 15-30.**

**Real Creator-plan concurrency ceiling: could not be authoritatively confirmed in this audit.** ElevenLabs' help-center pages return HTTP 403 to automated fetches; third-party sources disagree (~5 to ~15 concurrent requests depending on whether the source is describing the TTS API specifically vs. the Flash/Turbo-model-scaled table vs. the separate ElevenAgents/conversational product, which is a different limit entirely and not what this app uses). **This is the one number in this whole audit you should verify directly, in under 2 minutes, in your own account** — ElevenLabs' dashboard (Subscription/Usage page) shows your account's actual concurrency limit for your plan. Do this before deciding anything about upgrading.

```
Creator Plan:

Enough for current 3-user test?         UNCERTAIN — not the confirmed cause (Bug 1
                                         and Bug 2 explain the symptom without any
                                         concurrency pressure), but if your account's
                                         real ceiling is at the low end of the range
                                         found (~5), it is POSSIBLE for it to combine
                                         with concurrent testers/tabs/staff on the same
                                         key. Verify the real number (§13) before ruling
                                         this out completely.

Enough for 10 simultaneous users?       LIKELY YES, conditional on fixing Bug 1/2 first
                                         and on the app's own cap (10-15) actually being
                                         at or below your account's real ceiling.

Enough for 60-65 simultaneous users?    NO, not on the Creator plan as configured —
                                         even with the architecture's favorable duty-cycle
                                         math (§8), a burst where many students submit
                                         within the same second could exceed a ~10-15
                                         slot cap; matching your account's real ceiling to
                                         a validated, tuned app-side cap is required, and
                                         Creator-tier ceilings in every source found are
                                         well under what a 60-65 student burst could
                                         theoretically demand.
```

**Would upgrading the ElevenLabs plan alone fix today's problem? No.** Bug 1 and Bug 2 are pure frontend state-machine bugs that fire on a single transient hiccup, with zero dependency on how much ElevenLabs concurrency you're allowed. Paying for more concurrency would not stop one sentence's hiccup from disabling the rest of a reply, and would not stop a probe timeout from poisoning a whole turn before ElevenLabs is even contacted. **Fix the code first; only then does the ElevenLabs plan question become answerable from real data.**

---

# 7. Three-Student Failure Reconstruction

```
Student A: OpenAI streams "I understand. When did your pain begin? Does it hurt
           when you walk? Have you tried medication?" → 4 sentences extracted
           incrementally by streaming_engine.py as they complete (NOT pre-split,
           NOT generated in parallel — confirmed, one OpenAI call, sequential yield)
Student B: similar, 3 sentences
Student C: similar, 5 sentences

Per-student TTS request pattern (confirmed sequential, no Promise.all anywhere):
  A: A1 fetch → done → A2 fetch → done → A3 fetch → done → A4 fetch → done
  B: B1 fetch → done → B2 fetch → done → B3 fetch → done
  C: C1 fetch → done → C2 fetch → ... → C5 fetch → done

Realistic peak concurrency (worst case, all three happen to be mid-fetch at once):
  Time      Active ElevenLabs HTTP calls
  10:01.00  A1
  10:01.05  A1, B1
  10:01.20  A1, B1, C1        <- peak = 3, far under any plausible cap (10-15 app-
                                  side, likely 5-15 real ElevenLabs ceiling)
  10:01.60  B1 done → A2 starts (B1's sentence is now playing while B2 isn't
             fetched yet — playPump plays strictly in order, one at a time)
  ...

No capacity pressure occurs at 3 students under this architecture. What actually
happens instead (either bug can fire independently):

Path 1 (Bug 2 — probe):  Student A's turn N begins → GET /voice/status hiccups
  (backend/DB/network blip, NOT ElevenLabs) → available:false → voiceFailed=true
  BEFORE any sentence exists → ZERO ElevenLabs requests that whole turn →
  A hears an entirely robotic reply → turn N+1 re-probes fresh → ElevenLabs returns

Path 2 (Bug 1 — poisoning):  Student B's turn: B1 fetches fine via ElevenLabs → B2's
  fetch has a transient hiccup (mobile network blip, slow first byte, momentary
  409 from the semaphore) → AUDIO_FAILED dispatched → voiceFailed flips true for
  the WHOLE turn → B3 (and any further sentences) NEVER EVEN ATTEMPT ElevenLabs,
  despite ElevenLabs being completely healthy → B hears "realistic, robotic,
  robotic" for that turn → turn N+1 starts fresh → ElevenLabs returns for B1 again
```

**This reconstruction is the answer to "how can only 3 students still fail":** neither path requires more than one active student, let alone three. Three students simply give you three independent chances per round of exchanges for one of these to fire on somebody's turn — which is consistent with "it happens fairly often, but not to everyone, every time."

---

# 8. 60–65 Student Capacity Analysis

**Students ≠ ElevenLabs concurrent requests.** Because TTS is strictly sequential per student (confirmed, no parallel fan-out) and a turn cycle (think + speak + read + listen) is tens of seconds while an individual synthesis is roughly 0.4-1.5s, the fraction of time any one student is actually mid-synthesis is small (a rough, unmeasured estimate of single-digit percent — **needs a real load test to confirm, not asserted as fact here**).

```
1 active student  ≈ 0-1 simultaneous TTS requests (sequential, brief)
3 active students ≈ 0-3 simultaneous TTS requests, peak 3
10 active students ≈ low single digits steady, bursts toward 10 if many submit
                     within the same second
60 active students ≈ likely still single-to-low-double-digits steady state, but
                     BURSTS (many students starting/answering within the same
                     second — plausible in a classroom setting where an instructor
                     says "everyone start now") can genuinely approach or exceed a
                     10-15 slot cap. This is a real, code-supported risk at scale —
                     unlike the 3-student symptom, which is NOT a capacity problem.
```

| Students | Est. OpenAI concurrency | Est. TTS concurrency (steady/burst) | Risk |
|---:|---|---|---|
| 3 | ≤3 of 20 cap | ~0.3 / ≤3 | None from capacity. Bug 1/Bug 2 dominate — fix those first. |
| 10 | ≤10 of 20 | ~1 / ≤10 | Low; occasional robotic sentence from Bug 1/2 until fixed; capacity fine. |
| 20 | approaches 20 cap on bursts | ~2 / ≤15 (capped) | First waits appear on synchronized bursts (e.g. everyone starts together). |
| 40 | queues/503s past 20 cap | ~4 / 15 (capped) | Frequent browser-voice fallback on bursts unless TTS cap raised **and** matched to a verified real ElevenLabs ceiling. Single-worker/no-Redis infra (if still the case) becomes the dominant risk here, not ElevenLabs. |
| 60-65 | steady queueing past 20 cap unless raised | ~6-7 / 15 (capped) | Needs: multi-worker + Redis actually deployed (currently only documented, not shipped — §9), a verified ElevenLabs ceiling matched to a tuned cap, Bug 1/Bug 2 fixed (otherwise fallback rate is dominated by these bugs, not by real capacity), and a real-provider load test before trusting any of these numbers. |

Assumptions are explicitly estimates, not measurements — flagged per the audit's own rule. **A real-provider load test (not the simulated-AI mode) is required before this table can be trusted at the higher rows.**

---

# 9. AWS Analysis

Verified from the repo (not assumed):

- **Documented current deployment** (`docs/DEPLOYMENT.md:12-18`): `t3.micro` (2 vCPU burstable, ~1GB RAM), **1 Uvicorn worker**, **no Redis**, PostgreSQL on the same instance, Nginx "unchanged." **Documented planned**: larger instance (~4GB RAM+), **4 workers**, **Redis required**, Postgres still co-located (no migration planned even in the "planned" column).
- **What's actually in the repo as deployable artifacts:** only `backend/Dockerfile` and `backend/scripts/start.sh`, which runs `uvicorn app.main:app --host 0.0.0.0 --port ...` with **no `--workers` flag** → **1 worker, confirmed from the actual launch command**, not just from docs. No systemd unit, no `docker-compose.yml`, no `Procfile`, no Terraform/CloudFormation/Ansible, no CI/CD pipeline exist anywhere in the repo. The 4-worker systemd unit shown in `docs/DEPLOYMENT.md` is prose documentation only — **it is not applied anywhere in this codebase.**
- **Whether your actual production box is still a `t3.micro` running 1 worker with no Redis, or whether the "planned" migration already happened, is NOT determinable from the repository — needs production verification** (see §13 for exact commands).
- **Could EC2/server capacity alone explain "robotic at 3 students"?** Only indirectly, as an amplifier of Bug 1/Bug 2 (a throttled/starved box increases the odds of the one transient hiccup those bugs need). **It is not the root cause** — the root cause is the state-machine bugs in §3, which fire even on a perfectly healthy box given one unlucky network moment. Don't spend money on AWS scaling before fixing §3 — you'd likely still see the same symptom on a bigger box, just slightly less often.

---

# 10. Root Cause Ranking

```
1. Frontend whole-turn voiceFailed poisoning on a single sentence failure (§3, Bug 1)  — CONFIRMED
2. Frontend voiceFailed poisoning on a voice-status PROBE error, unrelated to
   ElevenLabs (§3, Bug 2)                                                              — CONFIRMED
3. Single-worker / no-Redis / co-located-Postgres current deployment amplifying
   the hiccup rate that triggers #1 and #2                                             — LIKELY (deployment specifics UNVERIFIED)
4. Nginx buffering the streamed audio/SSE, delaying first byte past the 10s
   frontend watchdog                                                                   — POSSIBLE (nginx.conf not in repo — UNVERIFIED)
5. Mobile autoplay/gesture-unlock failure                                              — UNLIKELY as a current bug (implementation is solid, covers typed + voice-mode entry points); mobile is worse only because it hits #1/#2 more often, not because unlock itself is broken
6. ElevenLabs Creator-plan concurrency ceiling too low for even 3 students'
   real traffic                                                                        — UNLIKELY as the primary cause (peak app-generated concurrency at 3 students is ≤3), POSSIBLE as a contributing factor if shared API-key usage (other tabs/staff/testers) coincides with testing, and the real ceiling is UNVERIFIED
7. Backend retry-inside-slot amplification / deferred cancellation holding
   slots longer than expected                                                          — POSSIBLE contributing factor, more relevant at scale than at 3 students
```

---

# 11. Recommended Fixes

```
P0 — before any further testing with real students:
  - Fix Bug 1 (§3): stop AUDIO_FAILED from setting the whole-turn voiceFailed flag.
    A correct version of this fix already exists, UNCOMMITTED, in your working tree
    (src/services/streamingQueueState.ts). Review it, run
    `node scripts/test-streaming-queue.mjs` (also already updated with regression
    tests for this exact bug), and ship it.
  - Fix Bug 2 (§3): stop treating a voice-status PROBE error as "ElevenLabs
    unavailable for this whole turn." Treat probe errors as "unknown, attempt
    ElevenLabs anyway" and let /synthesize's own per-sentence result be authoritative.
    This fix is NOT in the current working-tree draft — it still needs to be written.
  - Confirm, on the live box: is `OPENAI_PATIENT_STREAMING_ENABLED` actually true?
    (Code default is false — the atomic path makes Bug-1-equivalent failures affect
    the ENTIRE reply, not just remaining sentences, since it's one clip per turn.)
    Check via the admin AI Configuration screen or the live .env.

P1 — before scaling past current testing:
  - Ship voiceDiagnostics.ts events to a server endpoint (currently console-only,
    per §3 Bug 3) so a future incident is diagnosable from server logs instead of
    needing a student's phone in hand.
  - Verify Redis health and scope on the live box (GET /api/health should show
    "redis": "connected"; admin Traffic dashboard should show
    concurrency_scope: "global (redis)"). If Redis is required but flaky, either
    fix Redis or set REDIS_REQUIRED_FOR_CONCURRENCY=false for a single-worker box
    (per-process semaphore is exactly correct at N=1 anyway).
  - Confirm/obtain nginx.conf and check proxy_buffering for the two streaming
    routes (/api/voice/synthesize, /api/interviews/*/messages/stream).
  - Reconcile MAX_CONCURRENT_TTS_REQUESTS: code default 15 vs. .env.example's
    documented 10 — pick one deliberately, matched to your verified real
    ElevenLabs ceiling (see P0-equivalent action in §13).

P2 — before a 10-30 student real-provider load test:
  - Deploy the documented-but-not-shipped 4-worker + Redis + larger-instance plan
    (docs/DEPLOYMENT.md), or explicitly decide to stay single-worker longer.
  - Get the real ElevenLabs Creator-plan concurrency ceiling from your dashboard
    and set the app's cap to comfortably below it.

P3 — before 60-65:
  - Real-provider load test (not simulated mode) at each stage in §13's test plan.
  - Consider eleven_flash_v2_5 for lower first-byte latency (already anticipated
    in docs, not yet configured).
```

---

# 12. Upgrade Options

**Option A — Fix code only (Bug 1 + Bug 2), no ElevenLabs or AWS change.**
Expected reliability: should eliminate the large majority of "sentence 1 realistic, sentence 2+ robotic" and "whole reply suddenly robotic" reports at your current 3-student scale, since neither trigger needs concurrency. Expected capacity: still bounded by the single-worker/no-Redis infrastructure once you go beyond a handful of concurrent students. Remaining bottleneck: infra (§4 items 1-3) becomes the dominant remaining source of transient hiccups, plus the unverified ElevenLabs ceiling. This is the cheapest, fastest, highest-leverage fix and should happen regardless of any other decision.

**Option B — Fix code + verify/adjust ElevenLabs plan.**
Solves: confirms whether your app-side cap (10-15) is actually safe against your real account ceiling; removes the "POSSIBLE" item in §10 (#6) as a source of doubt. Does not by itself fix anything — Option A's code fixes are the load-bearing part. Only pursue a plan upgrade after confirming the real number and after A is shipped; don't buy capacity to fix a state-machine bug.

**Option C — Fix code + plan verification + AWS/infra scaling (4 workers + Redis + bigger instance + nginx tuning).**
Solves: the actual path to 60-65 concurrent students. Required regardless of the code fixes, because the documented "planned" infrastructure in `docs/DEPLOYMENT.md` has never actually been deployed (confirmed: current repo only ships a 1-worker, no-Redis configuration). This is necessary for scale, not for fixing today's 3-student symptom.

---

# 13. Production Test Plan & Required Evidence

**Before testing again, do these two checks (5 minutes total) — they resolve the two biggest "UNVERIFIED" items in this whole audit:**

```bash
# 1) Is Redis actually connected in production, and is streaming enabled?
curl -s https://<your-prod-host>/api/health | python3 -m json.tool
# Look for: "redis": "connected" (or similar) and any streaming-flag field.
# Also check the admin dashboard's "Traffic"/"AI Configuration" screens for
# concurrency_scope ("global (redis)" vs "per-process") and the streaming toggle.

# 2) How many uvicorn workers are actually running?
ps aux | grep uvicorn
# or, if using systemd:
systemctl status <your-service-name>
# Count worker processes. docs/DEPLOYMENT.md's "planned" config uses --workers 4;
# the repo's actual start.sh has no --workers flag (defaults to 1). This tells you
# which one is really deployed.
```

**ElevenLabs account check (2 minutes) — resolves §6/§10 item 6:**
Log into the ElevenLabs dashboard → Subscription/Usage. Look for the account's documented concurrent-request limit for your plan. This audit could not fetch that page programmatically (blocked); it is the one number in this report you should get directly rather than trust any third-party estimate.

**Server-side log collection during a 3-student test** (exact commands depend on how the process is supervised — confirm which applies to your box):

```bash
# If systemd:
journalctl -u <service-name> --since "10 minutes ago" | grep -E \
  "tts_browser_fallback|tts_provider_failure|voice_unavailable|tts_slot_timeout|redis_concurrency"

# If Docker:
docker logs <container> --since 10m 2>&1 | grep -E \
  "tts_browser_fallback|tts_provider_failure|voice_unavailable|tts_slot_timeout|redis_concurrency"

# Redis semaphore state, if Redis is in use (adjust key prefix to match
# distributed_semaphore.py's actual naming):
redis-cli --scan --pattern 'ptai:sem:*'
redis-cli zcard ptai:sem:tts     # current TTS slot occupancy, if that's the key
```

These backend log lines already exist and are classified (confirmed in code: `tts_browser_fallback reason=no_capacity_slot`, `redis_concurrency_blocked`, `redis_concurrency_local_fallback`) — the gap is purely that the *frontend's* equivalent events (voiceDiagnostics.ts) don't reach this same log stream yet (§3 Bug 3, §11 P1).

**Browser DevTools, per test device, during the 3-student test:**
- Network tab, filter `synthesize` and `status`: record HTTP status, duration, and response size for every call. A 409 means "no capacity slot" (not an ElevenLabs failure); a 502 means ElevenLabs itself failed; a slow-but-200 response followed by robotic voice anyway points at Bug 1/Bug 2 rather than ElevenLabs.
- Console: filter for `[voice]` (from `voiceDiagnostics.ts`, warn-level in production) — these lines directly name which of the categories in §3 fired, per sentence, with a `correlationId` you can cross-reference against server logs by turn/sentence index.

**Staged load plan:** 1 → 3 → 5 → 10 students, real-provider mode (confirm `SIMULATED_AI`/equivalent is OFF — a simulated run will hide all of this). At each stage, count: total `/synthesize` calls, count of 409 vs 502 vs 200-but-fallback (this last category proves Bug 1/Bug 2 rather than a provider or capacity issue), and `[voice] tts_browser_fallback` console occurrences with their `reason` field.

---

# 14. Supervisor Explanation

**Why is ElevenLabs "working sometimes, then robotic, then working again" with only 3 students, when we built this for 60-65?**

We found two bugs in our own frontend code — not in ElevenLabs, not in our server capacity. When the AI patient's reply is broken into several sentences and spoken one at a time, our code has a rule that says: *"if even one sentence has a brief hiccup getting its voice audio, give up on the realistic voice for every sentence after it in that reply."* A brief hiccup — a slow moment on ElevenLabs' side, a flicker in a phone's Wi-Fi/cell connection — is a normal, occasional thing at any user count. It doesn't require 3 students or 30; it can happen to a single student. When it happens, the rest of that one reply switches to the robotic system voice. The next time the student asks something, our code starts fresh, so it usually sounds normal again — which is exactly the "it comes back, and refreshing fixes it" pattern we were seeing.

There's a second version of the same class of bug that fires even earlier: before the AI even starts speaking, we do a quick internal check ("does this patient have a real voice configured?"). That check doesn't even talk to ElevenLabs — it's purely our own server. If that quick check itself is slow or hiccups, our code treats it exactly the same as "ElevenLabs is broken" and the whole reply goes robotic, again with no ElevenLabs involvement at all.

Mobile is worse for a simple reason: phones have more of exactly the kind of brief network hiccups that trigger these two bugs — switching towers, weaker signal, backgrounding — not because our mobile-specific code (which handles Apple's "you must tap before audio can play" restriction) is broken. We checked that part specifically and it's solid.

**Is the ElevenLabs Creator plan part of the problem?** Not for what we're seeing today. Our app only ever asks ElevenLabs for as many simultaneous voice clips as there are students actively mid-sentence at that instant — with 3 students, that's at most 3, nowhere near any plan's concurrency limit. We could not 100% confirm our Creator plan's exact limit (ElevenLabs' own pages blocked our automated check — it needs a 2-minute manual look at our dashboard), but even in the most conservative published estimate, 3 students isn't close to it. **Buying a bigger ElevenLabs plan would not fix today's symptom** — the bug is in our own code's decision logic, not in how much ElevenLabs capacity we're allowed.

**What has to happen before we try to scale to 60-65 students?** Two separate tracks: (1) fix these two frontend bugs — one fix is already partially written and sitting uncommitted in the codebase, it just needs to be finished, tested, and deployed; and (2) actually deploy the multi-worker, Redis-backed server setup that's currently only *documented* in our deployment notes but not yet running in production — today we're running a single server process with no shared coordination between workers, which is fine at 1 worker but was explicitly designed with 4 workers in mind. Both tracks are required for 60-65; only the first is required to stop today's 3-student symptom.
