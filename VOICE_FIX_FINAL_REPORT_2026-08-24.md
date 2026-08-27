# PT AI Patient — Atomic Voice Path Fix: Final Report (2026-08-24)

**Base commit:** `6c4a89921ab4cd400f2a2028fb28dcdaf3adce4c` (local `main`, matches the AWS-verified deployed commit from the earlier re-audit).
**Nothing committed, pushed, or deployed.** All changes are in the local working tree only.

---

## A. Current Production Path

Production runs with `OPENAI_PATIENT_STREAMING_ENABLED=false` (AWS-verified), so every student turn uses the **atomic** (non-streaming) voice path — `streamingQueueState.ts` / `patientStreamService.ts` are not on this path at all.

```
Student sends message
  → InterviewPage.performAtomicExchange()          src/pages/InterviewPage.tsx
  → POST /interviews/{sid}/messages                 (one JSON turn, whole reply text)
  → speakPatientResponse(options)                    src/services/patientVoiceService.ts:657

  speakPatientResponse():
    → getVoiceStatus(caseId)                         patientVoiceService.ts:164
        GET /api/voice/status/{case}                  backend/app/api/voice.py:47
        (local case-config lookup behind auth — NEVER calls ElevenLabs)
        Returns { available, fallbackRate, confirmed }.
        confirmed=true  → backend answered definitively (200 OK)
        confirmed=false → the PROBE itself failed (network/timeout/5xx) — unknown, NOT "unavailable"

    → shouldAttemptElevenLabs(status)                 src/services/voicePlaybackState.ts:79
        confirmed ? available : true   (an unconfirmed probe no longer skips ElevenLabs)

    → IF attempting ElevenLabs:
        playElevenLabs(options, ...)                   patientVoiceService.ts:270
          POST /api/voice/synthesize                    backend/app/api/voice.py:216
            → tts_slot().acquire() (Redis global semaphore, cap 15)
            → ElevenLabsClient.stream_speech()           backend/app/voice/elevenlabs_client.py
          response.ok? no  → VoiceStageError(stage:"tts_http")     [TTS_HTTP_ERROR]
          response.ok? yes → canUseMediaSource()?
            yes → playProgressive()  (MediaSource, chunk-by-chunk, 10s start watchdog)
                    decode/append failure  → VoiceStageError(stage:"audio_decode")  [AUDIO_DECODE_ERROR]
                    audio.play() rejected  → VoiceStageError(stage:"audio_play")    [AUDIO_PLAY_NOT_ALLOWED |
                                                                                       AUDIO_PLAY_ABORTED |
                                                                                       AUDIO_PLAY_UNKNOWN]
                    watchdog fires (no 'playing' in 10s) → VoiceStageError(stage:"audio_play") [TTS_TIMEOUT]
            no  → playBuffered()  (full Blob download, single Audio() play())

        on error, in speakPatientResponse's catch (patientVoiceService.ts:657):
          isFetchLevelVoiceError(err)?  (stage === "tts_http")
            true  → skip recovery, go straight to browser TTS (re-fetching an
                     identical request that already failed would just fail again)
            false → playElevenLabsBuffered(options,...)  patientVoiceService.ts:317
                     (re-fetch + full-Blob playback — recovers from a PLAYBACK
                      failure while the ElevenLabs generation itself was fine)
                     success → ElevenLabs voice preserved, browser TTS never reached
                     failure → falls through to browser TTS

    → IF NOT attempting ElevenLabs (status.confirmed && !status.available):
        skip straight to browser TTS — zero /synthesize calls

    → playBrowser(options, fallbackRate)               patientVoiceService.ts (browser speechSynthesis)
```

Mobile autoplay: `unlockAudioPlayback()` (`src/services/audioUnlock.ts`) fires from the voice-mode start tap, retry, **and** typed Send (`InterviewPage.tsx`) — every entry point that can trigger patient audio is covered. Confirmed solid in the earlier audit; unchanged here.

---

## B. Root Causes

| # | Cause | Verdict | Failure chain |
|---|---|---|---|
| 1 | **Voice-status probe error treated as confirmed "ElevenLabs unavailable"** | **CONFIRMED** (was live at `HEAD` before this fix) | `GET /voice/status` never touches ElevenLabs — any transient failure of that one lightweight, auth-gated backend call (not ElevenLabs) previously set the effective decision to browser-only for the **entire reply**, with zero ElevenLabs attempt. This is a pure backend/network hiccup on our own box being misreported as "ElevenLabs is down." |
| 2 | **Generation failure and playback failure were conflated in one catch** | **CONFIRMED** (was live at `HEAD`) | `playElevenLabs()` wrapped the HTTP fetch *and* the MediaSource/Blob playback in one try. A real ElevenLabs 5xx and a mobile `NotAllowedError` on `audio.play()` landed in the same catch and were both logged/treated as "ElevenLabs failed," with no attempt to recover audio that had, in fact, been generated successfully. |
| 3 | **`streamingQueueState.ts`'s whole-turn `voiceFailed` poisoning bug** | **REAL BUG, but NOT ACTIVE in current production** | Confirmed present at `HEAD` in the reducer (`AUDIO_FAILED` sets `voiceFailed: true`), but this reducer is only reachable via the streaming path (`patientStreamService.ts`), which requires `OPENAI_PATIENT_STREAMING_ENABLED=true`. Production has streaming **disabled**, so this code path is not exercised by students today. Already fixed in the uncommitted working tree (see §C) — kept as a forward-looking fix, not treated as today's root cause. |
| 4 | **ElevenLabs/AWS capacity at 3 users** | **NOT SUPPORTED** | Per-student TTS is sequential (no parallel fan-out anywhere in either voice path); 3 students generate at most ~3 concurrent ElevenLabs calls against an app-side cap of 15 and a Redis-global semaphore shared correctly across the 3 observed workers. No evidence of capacity pressure at this scale. |

---

## C. Existing Uncommitted Work (found before I changed anything)

`git status`/`git diff` at the start of this task showed:

| File | Purpose (as found) | Disposition |
|---|---|---|
| `src/services/streamingQueueState.ts` | Streaming-path fix: `AUDIO_FAILED` no longer sets whole-turn `voiceFailed` | **Kept, unmodified.** Correct fix for bug #3 above; verified by its own tests. |
| `src/services/patientStreamService.ts` | Streaming-path: bounded per-sentence retry, `voiceDiagnostics` logging | **Kept, unmodified.** Not on the live production path (streaming disabled), no reason to touch. |
| `src/services/patientVoiceService.ts` | Atomic-path: added a Blob-recovery attempt (`playElevenLabsBuffered`) before browser fallback, keyed on a string-prefix check (`isFetchLevelVoiceError`) | **Kept and built upon.** The recovery mechanism was correct; I replaced the string-sniffing classification with a typed `VoiceStageError` (stage-tagged) for robustness, and it is this file where the actual Bug 2 fix (§below) was added — this draft had **not** fixed Bug 2 yet. |
| `src/services/voiceDiagnostics.ts` (new, untracked) | Client-side classified event logging (console-only, in-memory counters) | **Kept and extended** with the new atomic-path stage events (see §E). |
| `scripts/test-streaming-queue.mjs` | Regression tests for the streaming-path fix | **Kept, unmodified.** |

None of this prior work was overwritten or discarded — I reviewed it, confirmed what it did and didn't cover, and built the atomic-path fix on top of it.

---

## D. Changes I Made

**`src/services/voicePlaybackState.ts`**
- Added `VoiceStatusResult` interface (`{ available, confirmed }`) and `shouldAttemptElevenLabs(status)`.
- Before: no way to distinguish "backend confirmed no voice" from "the probe itself failed."
- After: `shouldAttemptElevenLabs` returns `available` only when `confirmed` is true; otherwise always `true` (attempt ElevenLabs — `/synthesize` becomes the authoritative check).
- Why safe: pure, new, additive function; existing `chooseProvider` untouched and still exported/tested.

**`src/services/patientVoiceService.ts`** (the file that matters — this *is* the live production path)
- `getVoiceStatus()` (line 164): now returns `confirmed: true` only on an actual 200 response; a thrown/network error returns `confirmed: false` and is **never cached** (unchanged behavior there), with a new `voice_status_failed` / `voice_status_ok` / `voice_status_confirmed_unavailable` diagnostic event.
- `speakPatientResponse()` (line 657): replaced `chooseProvider(status.available) === "elevenlabs"` with `shouldAttemptElevenLabs(status)` — **this is the actual Bug 1 fix for the live path.** A transient probe failure no longer skips ElevenLabs.
- Added `VoiceStageError` (stage-tagged: `tts_http` / `audio_decode` / `audio_play`) replacing message-string sniffing (`isFetchLevelVoiceError` now checks `err.stage === "tts_http"` instead of matching on error-message text) — this is the Bug 2 fix: generation failures and playback failures are now structurally distinguished, not just by accident of wording.
- Added `classifyPlayRejection()` to label `NotAllowedError` / `AbortError` / other `audio.play()` rejections distinctly.
- Wired diagnostic events at every stage boundary (see §E) — the existing Blob-recovery mechanism (kept from the prior draft) is unchanged in behavior, now correctly triggered off the typed stage instead of a string prefix.
- Why safe: no behavior change to the happy path (ElevenLabs succeeds exactly as before); the only behavioral change is that an *unconfirmed* status probe no longer forces browser TTS — everything else is classification/logging on top of the existing control flow.

**`src/services/voiceDiagnostics.ts`**
- Added new event names and a `VoiceFailureCategory` type (additive; nothing removed, nothing renamed for the streaming path).

**`scripts/test-atomic-voice-fallback.mjs`** (new)
- 5 regression tests (A–E, see §F), wired into `npm run test:voice`.

**`scripts/test-progressive-playback.mjs`**
- One-line test-harness fix for a **pre-existing** hang, unrelated to this bug (see §F for proof it existed on unmodified `HEAD` too).

**Not touched:** `streamingQueueState.ts`, `patientStreamService.ts` (already fixed, not on the live path), any backend file, AWS/infra config, worker count, Redis, OpenAI/TTS caps, streaming flag, auth, assessment logic, database migrations, unrelated UI.

---

## E. Error Classification (as implemented)

```
STATUS_PROBE_TRANSIENT       - GET /voice/status itself failed (network/5xx/timeout).
                                Does NOT skip ElevenLabs.
STATUS_CONFIRMED_UNAVAILABLE - GET /voice/status returned 200 with available:false.
                                Skips ElevenLabs immediately (correct - no voice exists).
TTS_HTTP_ERROR                - POST /voice/synthesize returned non-2xx. Generation
                                failure; no Blob-recovery retry (would just fail again).
TTS_EMPTY_AUDIO                - synthesize returned 200 with a 0-byte body. Treated
                                like TTS_HTTP_ERROR (same skip-recovery logic).
TTS_TIMEOUT                    - progressive playback never reached 'playing' within
                                10s (existing watchdog). Treated as a playback-stage
                                failure - Blob recovery IS attempted.
AUDIO_DECODE_ERROR             - MediaSource/SourceBuffer append or decode failure,
                                or an <audio> element 'error' event. Blob recovery attempted.
AUDIO_PLAY_NOT_ALLOWED         - audio.play() rejected with NotAllowedError (autoplay
                                policy). Blob recovery attempted (a fresh element
                                sometimes succeeds where a MediaSource one didn't).
AUDIO_PLAY_ABORTED             - audio.play() rejected with AbortError (interruption -
                                not a real failure, informational).
AUDIO_PLAY_UNKNOWN             - any other play() rejection. Blob recovery attempted.
BROWSER_FALLBACK                - final drop to window.speechSynthesis, tagged with
                                WHY: "status_confirmed_unavailable" or "elevenlabs_failed".
```

Diagnostic events emitted (client-side, console + in-memory counters only — **not yet shipped to a server**, same gap as before): `voice_status_ok`, `voice_status_failed`, `voice_status_confirmed_unavailable`, `tts_request_started`, `tts_http_success`, `tts_http_failed`, `audio_blob_ready`, `audio_decode_failed`, `audio_play_started`, `audio_play_success`, `audio_play_failed`, `browser_fallback_started`.

---

## F. Test Results

**Tests added:** `scripts/test-atomic-voice-fallback.mjs` — 5 tests (A–E from spec), driving the REAL compiled `patientVoiceService.js` against DOM shims (MediaSource, Audio, fetch), same pattern as the existing `test-progressive-playback.mjs`.

| Test | Verifies | Result |
|---|---|---|
| A | Status-probe network error → ElevenLabs still attempted, not skipped | ✔ PASS |
| B | `audio.play()` rejects once → Blob recovery preserves ElevenLabs voice, browser never reached | ✔ PASS |
| C | Real ElevenLabs HTTP failure (502) → direct browser fallback, no pointless retry | ✔ PASS |
| D | A failed turn does not poison the next turn | ✔ PASS |
| E | Confirmed-unavailable case → immediate browser fallback, zero `/synthesize` calls | ✔ PASS |

**Full `npm run test:voice` suite: 66/66 PASS, ~67ms, clean exit.**

One **pre-existing** hang was found and fixed in the test *harness* (`test-progressive-playback.mjs`, unrelated to this bug — full root-cause analysis and proof against unmodified `HEAD` already given earlier in this session). No production code was changed to make any test pass.

**Existing relevant suites also re-verified individually:** `test-voice-state.mjs` (11/11), `test-interruption-judge.mjs` (8/8), `test-voice-playback.mjs` (18/18), `test-streaming-queue.mjs` (15/15) — all pass, all unaffected by this change.

---

## G. Risk Review

- **iPhone Safari:** Unlock mechanism unchanged and already solid (covers voice-mode start, retry, and typed Send). The fix reduces cases where a healthy ElevenLabs clip gets abandoned for a `NotAllowedError`, since Blob recovery is now attempted with a fresh `<audio>` element first. Residual risk: if the SECOND (recovery) attempt also hits a genuine autoplay block, it still correctly falls to browser TTS — no regression, same end state as before, just one extra recovery attempt in between.
- **Android Chrome:** Same mechanism; lower autoplay-restriction risk than iOS generally. No new risk introduced.
- **Laptop Chrome/Safari:** Least likely to be affected either way; happy path unchanged.
- **3 simultaneous users:** This fix does not touch concurrency, semaphores, or worker count — it only changes what a *single* transient hiccup does to a *single* turn. No new concurrency risk introduced.
- **General regression risk:** The riskiest change is the `shouldAttemptElevenLabs` swap — if `getVoiceStatus` somehow always threw (e.g., a genuine backend outage), ElevenLabs would now be attempted on every turn instead of being skipped, adding one extra `/synthesize` call per turn that will itself fail fast and fall to browser TTS. This is a bounded, self-limiting cost (one extra HTTP round-trip per turn during a real outage), not a correctness risk.

---

## H. What This Fix Does Not Prove

This fix addresses **today's reported 3-student symptom on the atomic path** — it does not, by itself, demonstrate that the system can reliably serve 60–65 concurrent students. Nothing here changes: EC2 sizing, worker count, Redis provisioning/health, ElevenLabs plan concurrency, OpenAI concurrency, Nginx buffering configuration, or database connection pooling. Confirming capacity at scale requires the staged, real-provider load test described in the earlier production audit (1 → 3 → 5 → 10 → 20 → 30 → 50 → 65 students), with server-side telemetry (this fix's diagnostic events are still console-only and have not been shipped to a server endpoint — that remains a P1 item).

---

## I. Deployment Readiness

**READY TO DEPLOY FOR CONTROLLED TESTING**

Rationale: the fix is minimal, isolated to the atomic voice path that is actually live in production, behaviorally identical on the happy path, backed by 5 new regression tests plus a clean full existing-suite run (66/66), and does not touch AWS/infra/concurrency/streaming configuration as instructed. It directly targets the two confirmed root causes for today's symptom (probe-error conflation, generation/playback conflation) without introducing new failure modes. Recommend deploying to the current controlled 3-student test group first, watching browser console `[voice]` events (now more informative) alongside the existing backend `tts_*`/`redis_*` logs, before considering any capacity-related change.
