# PT AI Patient — ElevenLabs Voice Reliability + 65–70-Student Scalability Audit

**Type:** Read-only source audit. No code, `.env`, AWS, or keys were changed.
**Basis:** The actual source under `backend/app/**` and `src/**` on this machine, read directly. Where a claim cannot be confirmed from the repository (production `.env`, Nginx config, ElevenLabs plan tier, live Redis state), it is marked **UNVERIFIED** and *why* is stated.
**Confidence tags:** every code-level conclusion is tagged **CONFIRMED** (read in source), **LIKELY** (strong inference from code + symptoms), or **UNVERIFIED** (depends on runtime config not in the repo).

> **One-line answer:** Your application is **not** fanning out ElevenLabs requests, and 3 students do **not** come close to your TTS concurrency cap (15 fleet-wide). The intermittent robotic voice at 3 students is an **infrastructure + transport + state-scoping** problem — a single burstable `t3.micro` running one worker (and Postgres) with streamed audio passing through Nginx, plus a few places where one transient hiccup flips a whole turn to the browser voice — **not** an ElevenLabs capacity problem. Buying more ElevenLabs concurrency would not fix it.

---

## 1. Executive Summary

### Why does ElevenLabs become robotic / intermittent with only 3 students?

The robotic voice is the app's **designed fallback** firing. Whenever a single `/api/voice/synthesize` request fails *or* is judged unavailable, the browser speaks that sentence (or that whole turn) with `window.speechSynthesis` — the robotic voice. So the real question is *"what is making individual synthesis attempts fail or time out intermittently at trivially low load?"* The evidence points away from ElevenLabs and toward the runtime:

**Confirmed causes (from code):**

1. **Per-student TTS is sequential, and 3 students cannot saturate the 15-slot cap.** The streaming client fetches one sentence at a time (`patientStreamService.ts:221` `fetchPump`, the `for(;;)` loop takes `pendingFetches(state)[0]` and awaits it). Three students → at most ~3 concurrent ElevenLabs requests against a fleet cap of 15 (`config.py:189`). **So saturation is impossible at 3 students.** *(CONFIRMED)*

2. **Several single-point transient failures flip an entire turn to the browser voice, then recover — which is exactly the "sometimes robotic, next turn fine, refresh fixes it" symptom.** The strongest are the voice-availability probe and the progressive-playback watchdog (see §5). *(CONFIRMED code paths; LIKELY the live trigger.)*

**Suspected causes (need one runtime check each — see §16):**

3. **`t3.micro` starvation (2 burstable vCPU, ~1 GB RAM, shared with PostgreSQL).** Under even light concurrent TLS streaming the box burns CPU credits, then throttles to its ~20%-of-2-vCPU baseline. A throttled box slows the *streamed audio read* enough to trip the **20 s** backend read timeout (`config.py:112`) or, far more often, the **10 s** frontend progressive-start watchdog (`patientVoiceService.ts:83`), which fails the sentence over to the browser voice. Intermittent by nature (depends on the CPU-credit balance and Postgres checkpoints). *(LIKELY — the current deploy is `t3.micro` per `docs/DEPLOYMENT.md:14`.)*

4. **Redis "fail-closed" deny, if Redis is configured but flaky.** In production the concurrency guard denies a TTS slot (→ 409 → browser voice) on *any* Redis blip, with 0.5 s socket timeouts (`distributed_semaphore.py:182-188`, `config.py:206-207`). A small/self-hosted Redis that stalls momentarily produces exactly this intermittent per-sentence robotic behavior. *(UNVERIFIED — depends on whether/how Redis is provisioned.)*

5. **Nginx buffering the streamed MP3/SSE.** With `proxy_buffering on` (the Nginx default) the progressive `MediaSource` path never receives early chunks, so the 10 s watchdog fires and the sentence degrades. The Nginx config is **not in the repo**. *(UNVERIFIED — but a classic cause of this precise symptom.)*

**Is ElevenLabs the bottleneck at 3 users?** No. *(CONFIRMED negative.)* It becomes *a* bottleneck only at fleet scale (≈15+ simultaneous sentence-synths), which 3 students never reach.

---

## 2. Current Architecture (confirmed from source)

### One student turn, end to end (streaming path — `OPENAI_PATIENT_STREAMING_ENABLED=true`)

```
STUDENT BROWSER (React SPA)
  │  types/speaks a question
  │
  ├── InterviewPage.performExchange()                     src/pages/InterviewPage.tsx:173
  │      fetchInterviewConfig() → streamingEnabled?  ── yes ─┐
  │                                                          ▼
  │   startStreamingExchange()                        src/services/patientStreamService.ts:139
  │      POST /api/interviews/{sid}/messages/stream  (SSE, one request per turn)
  │        signal = sseAbort
  ▼
NGINX  (reverse proxy on EC2; config NOT in repo)        proxy_pass 127.0.0.1:8000
  ▼
UVICORN — 1 worker (start.sh: uvicorn app.main:app, no --workers)   CONFIRMED single worker
  FastAPI, all route handlers are sync def → anyio threadpool (default 40 threads)
  ▼
POST /messages/stream  →  interview_stream_service.stream_student_message()   :280
  • reserve interview_slot  (DistributedSemaphore "openai_interview", cap 20)  concurrency.py:33
      – full → 503 ServiceOverloadedError BEFORE any provider call
  • short DB session loads context, then CLOSES before provider I/O            :87
  • streaming_engine.stream_patient_response()  → OpenAI Responses API (1 streamed call)
      – emits: speech event, then sentence events (index 0..N), then final
      – each sentence is safety-validated cumulatively before it is yielded    streaming_engine.py:221-247
  • commit ONE authoritative turn in a second short DB session                 :168
  ▼  SSE events stream back to the browser (event: speech | sentence | final | error)

BACK IN THE BROWSER — two independent pumps per turn (patientStreamService.ts):
  fetchPump  (SEQUENTIAL, one sentence at a time)          :221
     for each queued sentence:
        POST /api/voice/synthesize  {caseId, text, sessionId, turnId:"", speechStyle}
          │  up to 2 attempts (1 retry) on transient (net / 408/409/425/429/5xx)  :47,236-303
          ▼
        NGINX ▶ UVICORN ▶ voice.synthesize()               backend/app/api/voice.py:216
           • short DB session: auth + ownership + approved text + voice profile, then CLOSE  :144-213
           • audio_cache.get(key)  → HIT: return bytes (no provider call, not billed)         :236-239
           • tts_slot().acquire()  (DistributedSemaphore "tts", cap 15, wait ≤5 s)            :247
               – no slot within 5 s → 409 VoiceNotAvailableError → browser voice
           • ElevenLabsClient.stream_speech()  POST api.elevenlabs.io/v1/text-to-speech/{voice}/stream
               – model eleven_multilingual_v2, mp3_44100_128, streaming              elevenlabs_client.py:161
               – retry connect+first-chunk up to provider_max_retries=3, INSIDE the held slot  :178-199
           • StreamingResponse pipes MP3 chunks to the browser; slot released in finally        :313-334
     playPump  (STRICTLY ORDERED, no overlap)              :334
        plays sentence blobs in index order via MediaSource / <audio>; browser TTS for any
        sentence whose fetch failed (that sentence only)   streamingQueueState.ts:150-196
```

### Atomic path (`OPENAI_PATIENT_STREAMING_ENABLED=false` — the code default, `config.py:94`)

`performAtomicExchange()` (`InterviewPage.tsx:289`) → one JSON turn → `speakPatientResponse()` (`patientVoiceService.ts:566`) makes **one** `/synthesize` call for the **whole** reply, with a per-turn fallback chain: ElevenLabs progressive → ElevenLabs Blob → browser voice. Here a single failure makes the **entire reply** robotic (coarser than the streaming path's per-sentence fallback).

### State inventory (per the request)

| Concern | Where it lives | Scope |
|---|---|---|
| TTS concurrency slot | `DistributedSemaphore("tts")` Redis ZSET+Lua, else per-process | **Fleet-wide** (Redis) / per-worker (fallback) — `distributed_semaphore.py` |
| OpenAI interview slot | `DistributedSemaphore("openai_interview")`, cap 20 | Fleet-wide (Redis) / per-worker |
| Audio cache | `voice/audio_cache.py` OrderedDict, 24 entries | **Per worker, in-memory** (CONFIRMED) |
| ElevenLabs HTTP pool | module-level `httpx.Client`, keep-alive | Per worker (`elevenlabs_client.py:49`) |
| Rate limits / login throttle | `core/rate_limit.py` | **Per worker** (CONFIRMED, `config.py:148`) |
| Sentence queue + `voiceFailed` | `streamingQueueState.ts` reducer | **Per active turn**, in the browser |
| `voiceStatusCache` | `patientVoiceService.ts:101`, `patientStreamService.ts:121` | **Per browser tab** (cleared on refresh) |
| Playback generation guard | `voicePlaybackState` guard | Per browser tab |
| Redis | semaphores, interview waiting queue, worker heartbeats **only** — not caching/sessions | Fleet |
| PostgreSQL | users, sessions, turns, assessments, usage, runtime config | Global, durable |

**Deployment (from `docs/DEPLOYMENT.md`):** *Current* = `t3.micro`, 2 vCPU, ~1 GB RAM, **1 uvicorn worker**, **no Redis**, Postgres on the same box, Nginx unchanged. *Planned* = larger instance, **4 workers**, **Redis required**. **The fleet-wide machinery is built but, on the current box, largely dormant.** *(CONFIRMED from docs + `start.sh`; the live `.env` is UNVERIFIED.)*

---

## 3. Confirmed Bugs / Risks

Ranked by how likely each is to be *your* 3-student robotic-voice trigger. None were changed.

---

### V1 — Voice-availability probe failure silently robots an entire turn — CRITICAL (for this symptom)

- **Severity:** CRITICAL (highest-probability cause of intermittent robotic voice)
- **File / lines:** `src/services/patientStreamService.ts:123-137` and `:594-601`; mirrored in `src/services/patientVoiceService.ts:114-130` and `:574-577`.
- **Code:**
  ```ts
  async function getVoiceStatus(caseId) {
    const cached = voiceStatusCache.get(caseId);
    if (cached) return cached;
    try { … return entry; }
    catch { return { available: false, fallbackRate: 0.97 }; } // NOT cached: may recover
  }
  // begin():
  if (!status.available && !cancelled) { state = { ...state, voiceFailed: true }; }
  ```
- **Trigger:** The very first turn on a page calls `GET /api/voice/status/{case}`. If that one request is slow/times out/returns non-2xx (e.g. the `t3.micro` is mid-CPU-throttle, a Redis blip made the request 500, a transient network drop), the catch returns `available:false`.
- **What happens:** `voiceFailed = true` for that whole turn → `pendingFetches()` returns `[]` (`streamingQueueState.ts:213`) → **no ElevenLabs request is issued at all** → every sentence uses browser TTS. Because the failure is deliberately **not cached**, the *next* turn re-probes and usually succeeds → ElevenLabs returns. A page refresh clears the in-memory cache and re-probes cleanly.
- **Why it causes robotic voice:** an entire reply is spoken by the browser, from a single unrelated probe hiccup.
- **How to reproduce:** throttle `/api/voice/status` (or return 503 once) → that turn is fully robotic; next turn recovers.
- **Recommended fix (not applied):** treat a probe *error* as "unknown, attempt ElevenLabs anyway" rather than "unavailable" (only a definitive `available:false` from a 200 response should force browser voice); and/or make `/synthesize` itself the source of truth (it already degrades per-sentence). Log the probe failure as a distinct reason.

---

### V2 — Progressive-playback 10 s watchdog fails ElevenLabs over on any slow first chunk — HIGH

- **Severity:** HIGH
- **File / lines:** `src/services/patientVoiceService.ts:83` (`PROGRESSIVE_PLAYBACK_START_TIMEOUT_MS = 10_000`) and `:311-320`; interacts with backend read timeout `elevenlabs_timeout_seconds=20` (`config.py:112`).
- **Trigger:** On the atomic path (or any `MediaSource` playback), if actual playback hasn't begun within 10 s — because the box is throttled, Nginx is buffering, or ElevenLabs first-byte is slow — the watchdog calls `fail()`.
- **What happens:** the attempt fails → Blob retry → if that is also slow, browser voice. The MP3 may have been perfectly fine; it just didn't *start* in 10 s on a starved box/buffered proxy.
- **Why robotic:** a healthy-but-slow ElevenLabs clip is abandoned and the browser speaks instead.
- **Reproduce:** add ~11 s of first-byte latency (throttled CPU or `proxy_buffering on`) → watchdog fires → robotic.
- **Fix (not applied):** the timeout is reasonable, but on the streaming path the equivalent risk is smaller (per-sentence). Real fix is upstream: remove first-byte latency (multi-worker, no Nginx buffering for this route, faster model). Consider surfacing a `fallback_reason` so this is distinguishable from a true provider failure in logs.

---

### V3 — Single burstable `t3.micro` + 1 worker + co-located Postgres is the real ceiling — HIGH

- **Severity:** HIGH (root infrastructure cause; makes V1/V2 fire)
- **File / evidence:** `docs/DEPLOYMENT.md:12-18` (current = t3.micro/1 worker/no Redis); `backend/scripts/start.sh` (`uvicorn app.main:app` with **no `--workers`** → Uvicorn default 1); handlers are sync in a 40-thread anyio pool.
- **Trigger:** Concurrent TLS streaming (SSE + per-sentence MP3) on a 2-vCPU burstable instance that also runs PostgreSQL, on ~1 GB RAM. CPU credits deplete under sustained use; the box throttles to baseline; memory pressure from buffering audio.
- **What happens:** first-byte and streaming latency spike intermittently → trips V1/V2 → robotic voice; all endpoints slow.
- **Why robotic (indirectly):** it is the environmental cause that makes the two client-side timeouts fire at *3* students instead of 30.
- **Fix (not applied):** the *planned* config — larger instance (≥4 GB), **4 workers + Redis**, Postgres off the app CPU budget — exactly per `docs/DEPLOYMENT.md`. **This is the single highest-leverage change.**

---

### V4 — Redis fail-closed denies TTS slots on any blip (prod) — HIGH if Redis is the flaky kind

- **Severity:** HIGH (conditional) / UNVERIFIED trigger
- **File / lines:** `distributed_semaphore.py:178-209` (`_acquire_redis`: `ping_cached()` false or any Redis exception → `return None` when `redis_required`), `config.py:206-207` (0.5 s connect/socket timeouts), `config.py:361-369` (`redis_required` defaults true in prod/staging).
- **Trigger:** Redis is configured in prod but momentarily unreachable/slow (small self-hosted instance, network jitter, eviction pause).
- **What happens:** `tts_slot().acquire()` returns "no slot" → `409 VoiceNotAvailableError` → browser voice for whoever's sentence hit the blip. Intermittent and per-sentence — matches the symptom precisely.
- **Also:** if prod instead has **no** Redis and `ENVIRONMENT=production`, the app **refuses to boot** (`config.py:403`). So prod must currently run with `REDIS_REQUIRED_FOR_CONCURRENCY=false` **or** a non-strict `ENVIRONMENT`, **or** Redis is actually provisioned. **Which one is true is UNVERIFIED and worth confirming immediately** (`GET /api/health` shows `redis` status; the System Dashboard shows `concurrency_scope`).
- **Fix (not applied):** verify Redis health/latency; if flaky, either harden Redis or (for a single-worker box) set `REDIS_REQUIRED_FOR_CONCURRENCY=false` so a Redis blip falls back to the per-process semaphore instead of denying voice. With one worker the per-process limit is correct anyway.

---

### V5 — Nginx buffering breaks streamed audio/SSE — MEDIUM, UNVERIFIED

- **Severity:** MEDIUM / UNVERIFIED (config not in repo)
- **Evidence:** the app relies on true streaming (`StreamingResponse` of MP3, `text/event-stream` SSE, `X-Pause-Before-Ms` exposed at `main.py:106`). Nginx defaults `proxy_buffering on` and can buffer both, defeating progressive playback and delaying first byte past the V2 watchdog.
- **Fix (not applied):** for `/api/voice/synthesize` and `/api/interviews/*/messages/stream`, set `proxy_buffering off; proxy_read_timeout 120s; proxy_cache off; chunked_transfer_encoding on;` and ensure HTTP/1.1 to upstream. Confirm the current Nginx config.

---

### V6 — Streaming disabled in prod makes the whole reply robotic on one failure — MEDIUM

- **Severity:** MEDIUM / UNVERIFIED (depends on the runtime flag)
- **File:** `config.py:94` (`openai_patient_streaming_enabled=False` is the code default); `InterviewPage.tsx:187` picks atomic when `streamingEnabled` is false; atomic path speaks the whole reply as one clip (`patientVoiceService.ts:566`).
- **Impact:** on the atomic path, one failed `/synthesize` = the *entire* patient reply robotic (vs one sentence on the streaming path). Coarser, more noticeable.
- **Fix (not applied):** confirm whether streaming is enabled in prod (Admin → AI Configuration shows it). Enabling it both lowers latency *and* makes voice degradation per-sentence instead of per-turn. (Also fixes B3 below.)

---

### V7 — ElevenLabs retries run *inside the held TTS slot* and can pin a slot ~12 s+ — MEDIUM

- **Severity:** MEDIUM (a scale concern, not a 3-student concern)
- **File:** `elevenlabs_client.py:178-199` (retry loop inside `stream_speech`), called while the slot from `voice.py:247` is held.
- **Detail:** `provider_max_retries=3`, backoff `200ms·2^n + jitter`, capped 4000 ms; each attempt has a 20 s read budget. This is **good** for provider protection — retries occupy the *same* slot, so they never amplify concurrency against ElevenLabs (confirmed no retry-storm). But a persistently-slow ElevenLabs can hold one of the 15 slots for the full retry envelope, shrinking effective capacity under a real provider incident.
- **Fix (not applied):** cap total in-slot time (e.g. a hard deadline across attempts), or reduce retries to 1–2 for TTS specifically. Low priority.

---

### V8 — Per-worker audio cache dilutes hit rate at multi-worker scale — LOW

- **Severity:** LOW
- **File:** `audio_cache.py` (24-entry in-memory OrderedDict, per process).
- **Impact:** with 4 workers the same greeting can miss cache on 3 of 4 workers → more billed characters + more first-byte latency than necessary. Not a correctness issue.
- **Fix (not applied):** raise `elevenlabs_cache_max_entries`, and/or move the audio cache to Redis so it is shared and survives restarts. Greetings/opening lines are highly cacheable.

---

### Also inherited from the prior backend audit (still valid, cross-checked)

`B1` global `mock_ai` flag from a simulated load test poisons live users (HIGH operational — never run load tests against prod while students are active); `B3` non-streaming path holds a DB session across the OpenAI call; `B5` `MAX_CONCURRENT_TTS_REQUESTS` code default (15) disagrees with `.env.example`/docs (10). All **CONFIRMED**.

---

## 4. ElevenLabs Concurrency Analysis — what our app's real TTS concurrency is

**Real concurrent ElevenLabs HTTP requests ≠ students × sentences.** Two structural facts bound it:

1. **Per student, TTS is strictly sequential.** `fetchPump` (`patientStreamService.ts:221`) processes `pendingFetches(state)[0]`, awaits the full fetch, then loops. There is **no `Promise.all`, no `p-limit`, no parallel map** anywhere in the voice path. *(CONFIRMED — grep of the voice services shows sequential pumps only.)* So a 6-sentence reply is **6 sequential** requests for that student, never 6 concurrent.

2. **Fleet-wide, a hard cap of `MAX_CONCURRENT_TTS_REQUESTS` (15 default) admits requests**; the 16th waits ≤5 s then degrades to browser voice (`concurrency.py:103-130`, `voice.py:247-250`).

So the real peak concurrency is **the number of students who happen to be inside an active synthesis fetch at the same instant** — a function of each student's *duty cycle*, not their sentence count:

```
peak_concurrent_TTS  ≈  active_students × P(student is mid-synthesis right now)
```

A student's turn cycle is dominated by thinking, typing/speaking, reading, and *listening* to the reply (audio plays while no new synth is happening for that student). First-byte + short generation per sentence is on the order of ~0.4–1.5 s; a full turn cycle is tens of seconds. So the per-student duty cycle in "actively synthesizing" is roughly **5–15%**. That is why the 15-slot cap can, with even scheduling, serve far more than 15 students — and why **3 students (peak ~3, steady ~0.3) never approach it.**

The **burst** case (everyone submits within the same second) is the real risk: momentarily `min(active_students, 15)` requests contend, and beyond 15 the overflow degrades to browser voice for a sentence. That is a ~70-student concern, not a 3-student one.

---

## 5. Frontend Voice State-Machine Analysis

The sentence queue is a **pure, well-designed reducer** (`streamingQueueState.ts`) and is **not** the poison source people feared. Verified state transitions:

```
queued ──FETCH_STARTED──▶ fetching ──AUDIO_READY──▶ ready ──PLAY_STARTED──▶ playing ──PLAY_ENDED──▶ done
                                   └──AUDIO_FAILED──▶ failed ───────────────▶ (browser TTS, in order) ──▶ done
CANCEL (terminal, absorbs all later events at any phase)
```

Key correctness properties, all **CONFIRMED** in code:

- **`AUDIO_FAILED` marks ONLY that sentence** (`:150-158`) and explicitly does **not** set the global `voiceFailed`. So a single transient sentence failure **never** disables ElevenLabs for later sentences — the reducer comment and the code agree. *(This is the opposite of the "one error poisons the turn" hypothesis in your Part 3 — that anti-pattern is NOT present in the reducer.)*
- **`voiceFailed` is set in exactly one place:** `begin()` when the up-front availability probe says the case has no voice (`patientStreamService.ts:598-600`). The danger is not the flag's *scope* (it is correctly whole-turn); it is that a **transient probe error is treated as "unavailable"** (bug **V1** above).
- **Exactly-once & ordering:** duplicate `ADD_SENTENCE` indices are ignored (`:129`); only `nextToPlay` may start and only if nothing is playing (`:159-167`); `pendingFetches` excludes cancelled/failed/voiceFailed (`:213`). No sentence repeats, no overlap.
- **`pendingFetches` is NOT a poisoning map.** It is a derived selector over queued sentences, not a stuck Set of in-flight promises. There is no equivalent of a `pendingFetches` map that would block a re-request. *(CONFIRMED — the name in your prompt refers to this pure selector, which is safe.)*

**Race/poison findings:**

- **No permanent poisoning inside a turn.** Across turns, the only cross-turn state is `voiceStatusCache` (per tab). A cached *success* is sticky for the tab's life (good); a *failure* is never cached (so it self-heals next turn / on refresh). This is why **refresh "fixes" it** — it is a symptom of the transient-probe bug (V1), not of a corrupted queue.
- **Autoplay/`play()` rejection is correctly isolated:** `playPump` catches `audio.play()` rejection and falls back to browser TTS **for that sentence only** (`patientStreamService.ts:401-418`), keeping ElevenLabs for the rest. This is the *right* design and must be preserved.

**Verdict:** the state machine is sound. The robotic voice comes from *inputs* to it (probe error → `voiceFailed`; fetch 409/timeout → `AUDIO_FAILED`), driven by infrastructure, not from a bug in the reducer.

---

## 6. Backend TTS Analysis

- **HTTP client:** one shared keep-alive `httpx.Client` per worker, pool sized `max(15, 8)` so a slot-winner never re-queues in httpx (`elevenlabs_client.py:53-89`). Good.
- **Timeouts:** connect 10 s, read = `elevenlabs_timeout_seconds=20`, write 10 s, pool 10 s (`elevenlabs_client.py:74-82`, `config.py:112,125`). Reasonable, but 20 s read on a throttled box is long enough that the frontend watchdog usually fires first.
- **Retries:** transient-only (429/5xx/timeout/empty), exp backoff+jitter, **inside the held slot** (no amplification), mid-stream never retried (`elevenlabs_client.py:178-245`). Strong.
- **Semaphore:** fleet-wide Redis ZSET+Lua with a 180 s lease so a crashed holder self-expires (`distributed_semaphore.py`). Correct and honest.
- **Workers:** **one** today (`start.sh`), so the fleet machinery is dormant; with the planned 4 workers the Redis cap becomes the true fleet cap (that is the whole reason Redis is required — `config.py:403`).
- **Redis:** used only for semaphores/queue/heartbeat; **fail-closed** in prod (deny = browser voice). 0.5 s timeouts mean a blip denies a slot rather than hanging (V4).
- **API keys:** single key from runtime config/`ELEVENLABS_API_KEY`, sent only as `xi-api-key`, never logged, shared across the worker (`elevenlabs_client.py:162`). No key rotation, no multi-key pool. 429s are **not** interpreted as a permanent voice failure — they are retried, then degrade per-request (`_failure_category` / `_TransientTTSError`, `:106-115,226-236`). Good.
- **Error handling:** categorized (timeout/auth/rate_limit/connection/api) and logged server-side; the browser only ever sees a generic 409/502. Good, but the frontend can't tell *which* category caused a fallback (see §15).
- **DB discipline:** no DB session is held across the provider call or the audio stream — two short sessions bracket the network I/O (`voice.py:144-213, 294-311`). Excellent.

---

## 7. Browser / Mobile Playback Analysis (independent of ElevenLabs)

A student hearing the robotic voice does **not** prove ElevenLabs failed. Confirmed client-side failure surfaces that are **not** provider failures:

- **iOS autoplay gating:** patient audio is produced *after* an `await` (the backend round-trip), which breaks the gesture chain. The app handles this with an explicit unlock on the start tap — AudioContext resume + silent WAV + primed `speechSynthesis` (`audioUnlock.ts:59-111`), re-armed on `retry()` and on foreground (`useVoiceConversation.ts:365,404,469`). Solid, but if a student's first audio is triggered *without* a preceding gesture (e.g. typed chat with voice on, no prior tap), `audio.play()` can still reject → that sentence goes browser voice. *(CONFIRMED path; LIKELY on iOS Safari in typed-with-voice mode.)*
- **`play()` rejection / decode error:** handled per-sentence (streaming) or per-turn→Blob→browser (atomic). Correctly logged as `tts_audio_play_failed` with `reason: play_rejected | media_error` (`patientStreamService.ts:389-418`, `patientVoiceService.ts:413-419,533-546`).
- **`MediaSource('audio/mpeg')` unsupported:** falls back to full-Blob playback keeping the ElevenLabs voice (`patientVoiceService.ts:235-242`). Only if *that* fails does it go robotic.
- **AudioContext suspended after backgrounding:** resumed on visibility change; conversation auto-pauses when hidden (`useVoiceConversation.ts:459-477`). Good.
- **`playsinline` / muted unlock:** set on the silent primer (`audioUnlock.ts:82-83`).

**These four categories (autoplay, play-reject, decode, MediaSource-unsupported) must be counted separately from provider failures.** Today they *are* logged with distinct events/reasons in the browser console — but those logs don't reach a server (see §15), so operationally they're invisible.

---

## 8. Retry Amplification Analysis

**Logical vs physical request count, worst case, per sentence:**

| Layer | Retries | Multiplier |
|---|---|---|
| Frontend `fetchPump` | 1 retry, transient only, 150 ms backoff (`patientStreamService.ts:236`) | ×2 |
| Backend→ElevenLabs `stream_speech` | up to `provider_max_retries=3` (4 attempts), **inside the held slot** (`elevenlabs_client.py:178`) | ×4 |
| OpenAI (separate call, not TTS) | `provider_retry`, ≤3, inside interview slot | n/a for TTS |

- **Physical ElevenLabs requests per logical sentence:** worst case **2 × 4 = 8** attempts — but only under sustained transient failure, and **the 4 backend attempts share one TTS slot**, so they do **not** raise concurrency against ElevenLabs. *(CONFIRMED — this is the key anti-storm property.)*
- **No nested client retry loops beyond these two layers.** The streaming→atomic fallback in `InterviewPage.tsx:187-205` happens **once** with the same idempotent `clientTurnId` (no duplicate save/regeneration).
- **Amplification against the provider is structurally prevented** by "retry inside the slot + shed-on-full." The only residual amplification is *client request volume* on 409/timeout, which the backend absorbs as cheap 409/503s — not extra provider calls.

**Verdict:** retries are not causing your 3-student problem. They add latency during a real provider incident, nothing more.

---

## 9. Why 3 Students Can Fail — numeric walk-through (our implementation)

Assume the streaming path, a 4-sentence reply, `t3.micro`, 15-slot TTS cap.

```
Request accounting for 3 students, worst realistic burst (all submit within ~1 s):
  OpenAI streamed calls in flight        = 3        (cap 20 → fine)
  Peak concurrent ElevenLabs requests    = 3        (each student sequential; cap 15 → fine)
  Steady-state concurrent ElevenLabs     ≈ 0.3–1    (5–15% duty cycle × 3)
```

There is **no capacity pressure whatsoever.** So the robotic voice at 3 students is produced by *one* of these single-request events, any of which flips a sentence/turn to browser voice:

```
Path A (probe):    turn N's GET /voice/status is slow on the throttled t3.micro
                     → catch → available:false → voiceFailed=true
                     → ZERO ElevenLabs requests that turn → whole reply robotic
                     → turn N+1 re-probes OK → ElevenLabs returns   (V1)

Path B (watchdog): sentence's /synthesize first byte > 10 s (CPU throttle OR Nginx buffering)
                     → progressive watchdog fail() → Blob retry slow too → browser voice   (V2/V5)

Path C (redis):    /synthesize hits a 0.5 s Redis timeout during a blip
                     → tts_slot denies (fail-closed) → 409 → browser voice for that sentence  (V4)

Path D (io):       ElevenLabs first-chunk read exceeds 20 s on the starved box
                     → _TransientTTSError → retried inside slot → still slow → 502 → browser  (V2/V3)
```

Every path is **intermittent** (depends on the box's instantaneous CPU-credit balance / a Redis or network jitter), **per-sentence or per-turn**, and **self-healing next turn / on refresh** — which is precisely the reported behavior. The common denominator is a **starved single box and/or a flaky fleet dependency**, *not* ElevenLabs concurrency.

---

## 10. Can the Current Architecture Support 70 Students?

**NOT SAFELY — on the current `t3.micro` / 1-worker / (no or flaky) Redis deployment.**

- **Software design:** genuinely capable of ~65–70 with the *planned* config (sequential per-student TTS, fleet semaphores, shed-on-full, per-sentence fallback, short DB sessions, idempotent turns). The architecture is right.
- **Current runtime:** one burstable 2-vCPU / ~1 GB box also running Postgres, single worker, cannot hold 70 concurrent SSE + audio streams without severe latency and memory pressure. It is already tripping client timeouts at 3.
- **Fleet caps:** 15 TTS / 20 OpenAI are reasonable *starting points* for ~70 with even arrival, but must be validated against your **real ElevenLabs plan concurrency** (UNVERIFIED) and OpenAI tier, and bursts need queue/backpressure UX.

**Answer: NOT SAFELY as deployed today; YES-with-conditions on the planned 4-worker + Redis + larger-instance config, once validated by real-provider load runs.**

---

## 11. Recommended Target Architecture (for 65–70 active students)

```
                         STUDENT BROWSERS (React SPA)  — per student: sequential per-sentence TTS
                                        │  HTTPS
                                        ▼
                       NGINX  (proxy_buffering OFF for /voice/synthesize + /messages/stream,
                               HTTP/1.1 upstream, keepalive, TLS)
                                        │
                                        ▼
        UVICORN — 4 workers on a ≥4 GB instance (Postgres on its own instance/RDS)
        every request sync in a tuned anyio threadpool
                    │
     ┌──────────────┼───────────────────────────────┬─────────────────────────────┐
     ▼              ▼                                ▼                             ▼
  OpenAI slot   TTS SCHEDULER                   REDIS (shared)               PostgreSQL
  (fleet 20)    ├─ fleet TTS semaphore (cap = min(plan, tuned))  • ptai:sem:*   users/turns/
                ├─ per-session fairness (round-robin, cap 1–2 in-flight/student)  assessments
                ├─ FIRST-SENTENCE priority lane                  • ptai:iq:*    (own instance)
                ├─ bounded queue + backpressure (≤5 s → browser)  • worker hb
                └─ SHARED audio cache (Redis) for greetings/openers
                    │
                    ▼
             ElevenLabs (single key; add key-pool / provider failover ONLY if the
             real plan concurrency is proven insufficient after scheduling)
```

**What to adopt (in priority order):**

1. **Planned infra first** — 4 workers, Redis, ≥4 GB instance, Postgres off the app CPU budget, Nginx no-buffering for streaming routes. This alone likely resolves the 3-student symptom.
2. **First-sentence priority** — synthesize sentence 0 in a high-priority path so perceived latency stays low even when later sentences queue (see §12). The pipeline already emits sentence 0 first; add a priority lane so it never waits behind other students' tail sentences.
3. **Per-session fairness** — cap in-flight TTS at 1–2 per student (already ~1 by the sequential pump) and round-robin across students so one verbose reply can't starve others.
4. **Bounded queue + explicit backpressure** — keep the ≤5 s wait-then-browser degrade, but log `VOICE_QUEUE_TIMEOUT` vs `VOICE_QUEUE_OVERFLOW` distinctly.
5. **Shared audio cache in Redis** — dedupe greetings/opening lines across workers and students; big cost + latency win.
6. **Circuit breaker (optional)** — during a real ElevenLabs outage, skip straight to browser voice for a cooldown window instead of paying retry latency every sentence.
7. **Faster model** — `eleven_flash_v2_5` (the config comments already anticipate it) cuts first-byte latency, shrinking the window in which V2's watchdog can fire.
8. **Multi-key / provider failover** — **only** if, after 1–6, real-plan concurrency is proven the binding constraint. Do not start here.

---

## 12. First-Sentence Prioritization

**Yes — worth doing; it reduces both perceived latency and burst pressure.** *(LIKELY beneficial; CONFIRMED the pipeline already supports it.)*

The streaming engine already yields sentence 0 first (`streaming_engine.py:247`) and the frontend plays it before later sentences are generated. To make this robust at scale:

```
OpenAI stream → sentence 0 ──▶ HIGH-PRIORITY TTS lane (never queues behind other students' tails)
                              ──▶ play immediately (student hears a voice ~first-byte fast)
             → sentences 1..N ─▶ normal lane, synthesized while sentence 0 plays
```

- **Perceived latency:** the student hears the patient begin as soon as sentence 0's first byte arrives — the rest is masked by playback of sentence 0.
- **Burst pressure:** because later sentences are generated *during* playback (tens of seconds of natural spacing), they spread across time rather than all hitting ElevenLabs at once. This is the opposite of "generate all sentences simultaneously," which the code already avoids (no `Promise.all`).

**Do not** synthesize all sentences up front. The current sequential pump is correct; the only addition is a priority lane for index 0 so it wins the semaphore under contention.

---

## 13. Estimated Capacity

Assumptions (stated, not measured — real-provider load runs required): streaming ON, Redis up, planned 4-worker/≥4 GB box, ~4 sentences/reply, per-student duty cycle ~10%, ElevenLabs first-byte ~0.4–1.5 s. Peak TTS ≈ students × duty-cycle, with bursts to `min(students,15)`.

| Students | OpenAI concurrent demand | TTS requests (steady / burst) | Peak TTS concurrency | Expected queue | Risk |
|---:|---|---|---|---|---|
| 3 | ≤3 of 20 | ~0.3 / ≤3 | ≤3 | none | none — robotic voice here = infra/state bug, not capacity |
| 10 | ≤10 of 20 | ~1 / ≤10 | ≤10 | rare | low; occasional robotic sentence on a burst |
| 20 | ~at 20 cap on bursts | ~2 / ≤15 | ≤15 | short (≤2 s OpenAI) | first waits; some browser-voice on synchronized bursts |
| 40 | queue/503 past 20 | ~4 / 15 (capped) | 15 | moderate | frequent browser voice on bursts unless TTS cap raised + plan supports |
| 70 | steady queue past 20 | ~7 / 15 (capped) | 15 | noticeable | works *if* burst UX + cap tuned; voice robotic on synchronized submits without first-sentence priority |

**Current-architecture safe capacity:** **~3–5 active students** (empirically already failing at 3 due to infra/state bugs) — call it **≤5** until V1–V5 addressed. *(LIKELY.)*

**After software fixes only (V1, V2, V4/V6 flags; same t3.micro):** **~8–12** — removes the spurious fallbacks, but the box is still the ceiling. *(ESTIMATE.)*

**After proper TTS queue/concurrency architecture (planned 4-worker + Redis + ≥4 GB + Nginx no-buffer + first-sentence priority + shared cache):** **~65–70**, pending real-provider validation. *(ESTIMATE — do not claim as fact until a real run passes.)*

**Infrastructure / account upgrades actually required for 70:**
1. Instance ≥4 GB RAM, ≥2 real (non-throttled) vCPU, **4 uvicorn workers**.
2. **Redis** provisioned and healthy (fleet semaphores + shared cache).
3. **PostgreSQL** off the app box (or RDS), `max_connections` ≥ `4×(5+10)+headroom`.
4. **Nginx** streaming-friendly config (no buffering on the two streaming routes).
5. **ElevenLabs plan** whose real concurrent-request allowance ≥ your tuned cap (verify the *actual* number, not marketing — **UNVERIFIED**). With first-sentence priority + duty-cycle spreading, ~15 concurrent is plausibly enough for 70; confirm empirically.
6. **OpenAI tier** TPM/RPM matching `OPENAI_TPM_LIMIT/RPM` (defaults 250k/3k).

---

## 14. Load-Test Plan

**Do not** fire 70 raw HTTP requests. Simulate real students (the repo's `load_tests/worker.py` `streaming_voice` mode already does: login → cases → session → N turns with think-time → **one sequential `/voice/synthesize` per sentence** → complete → assessment poll). **Critically, run in REAL-provider mode against production-class hardware** — the default `SIMULATED_AI` mode replaces OpenAI/ElevenLabs with fixed-latency stubs and will *hide* this entire class of problem (a 70-user simulated PASS proves plumbing only). Real-provider runs are capped at 10 users (`load_test_real_provider_max_users`, `config.py:292`) — raise deliberately and watch spend.

**Stages:** 1 → 3 → 5 → 10 → 20 → 30 → 50 → 70 (ramp), then a **spike** job (many arrive within ~1 s) to expose burst degradation.

**Per stage, record:** OpenAI p50/p95/p99; TTS queue-wait p50/p95/p99 (`tts_capacity()` exposes `wait_p50/p95`, `timeouts_5m`, `waited_5m`); ElevenLabs p50/p95/p99 + 429/timeout/5xx counts (telemetry `elevenlabs.window`: `rate_limited`, `retries`, `degraded`); **robotic-fallback %** and **lost-sentence count** (needs the client→server telemetry in §15); first-audio latency; successful-ElevenLabs %; Redis errors/latency; CPU (credit balance!) / RAM; worker utilization; DB pool checkouts.

**Acceptance criteria (recommended thresholds for this architecture):**
- ElevenLabs voice success **> 98–99%** of sentences at the target stage.
- Robotic fallback **< 1–2%** steady state (bursts may briefly exceed).
- **Zero** lost sentences; **zero** permanent voice poisoning (voiceFailed never sticks across turns).
- p95 **first-audio latency ≤ ~2.0 s** (matches `patient_streaming_first_audio_target_ms=2000`, `config.py:103`).
- No 5xx from resource exhaustion; CPU credits not depleted at steady state.

Gate each production stage on the prior stage passing (see §16 fix priority).

---

## 15. Production Observability Plan

**Today:** the backend logs are good (`tts_browser_fallback reason=no_capacity_slot`, `tts_provider_failure`, `voice_unavailable`, `tts_slot_timeout`, plus telemetry counters `degraded/retries/rate_limited` and `tts_capacity()` percentiles). **But the frontend fallback events (`voiceDiagnostics.ts`: `tts_fetch_failed`, `tts_audio_play_failed`, `tts_progressive_start_timeout`, `tts_browser_fallback`, …) are logged only to the browser console** — they never reach a server. So you **cannot** currently answer *"why did Student 2 hear robotic voice at 10:32:18?"* without that student's console. That is the single biggest observability gap.

**To make every robotic-fallback event answerable, ship a small client→backend telemetry beacon** (a `POST /api/voice/telemetry` that stores the existing `voiceDiagnostics` events) carrying, per event:

```
request_id / correlationId (already exists: `${turn}:s${index}`)   session_id   student_id_hash
turn_id   sentence_id (index)   voice_id/case_id   worker_id   fallback_reason
tts_queue_wait_ms   elevenlabs_request_ms   http_status   retry_count   audio_bytes
frontend_fetch_ms   playback_start_ms   path (progressive|blob|browser)   provider (elevenlabs|browser)
```

Then **join** it with the backend's server-side TTS logs on `correlationId` so each robotic event is classified into the taxonomy below. Add dashboard tiles for: robotic-fallback %, fallback-by-reason breakdown, ElevenLabs success %, TTS queue-wait p95, Redis deny count, and per-worker vs fleet scope (already honest in `tts_capacity()`).

---

## 16. Required Error Classification + Final Recommendation

### Error taxonomy (every robotic fallback should carry exactly one reason)

Split into the four categories your Part 4 asked for — **TTS generation** vs **transport** vs **browser playback** vs **application state** — because each needs a different fix:

| Code | Category | Where detected today |
|---|---|---|
| `ELEVENLABS_429` | A. generation | `elevenlabs_client.py:226` (rate_limited) |
| `ELEVENLABS_5XX` | A. generation | `elevenlabs_client.py:226` |
| `ELEVENLABS_TIMEOUT` | A. generation | `_failure_category` timeout (`:107`) |
| `ELEVENLABS_NETWORK_ERROR` | A. generation | `httpx.TransportError` (`:113`) |
| `VOICE_QUEUE_TIMEOUT` / `VOICE_QUEUE_OVERFLOW` | A. admission | `voice.py:247` 409 / `tts_slot_timeout` |
| `REDIS_LIMITER_FAILURE` (fail-closed deny) | A. admission | `distributed_semaphore.py:184` |
| `AUDIO_FETCH_ABORTED` | B. transport | frontend AbortError (cancel — normal) |
| `TRANSPORT_STREAM_CUT` (proxy buffering/reset) | B. transport | frontend read error / watchdog |
| `AUDIO_PLAY_REJECTED` / `MOBILE_AUDIO_LOCKED` | C. playback | `patientStreamService.ts:401`, `patientVoiceService.ts:540` |
| `AUDIO_DECODE_FAILED` | C. playback | `audio.onerror` media_error |
| `PROGRESSIVE_START_TIMEOUT` | C. playback | `patientVoiceService.ts:311` watchdog |
| `FALLBACK_STATE_VOICEFAILED_PROBE` | D. state | `patientStreamService.ts:598` (bug V1) |

Today these mostly exist as distinct log *strings* but are not centrally aggregated (§15). The rule: **a robotic sentence with reason in category A** = real provider/admission issue (scale/plan/Redis); **B** = fix Nginx/network; **C** = fix client audio/unlock; **D** = fix the probe logic. Do **not** treat C/D as "ElevenLabs failed."

### Final recommendation — answers to your 12 questions

1. **Why can 3 users already trigger robotic voice?** Not capacity. A single transient event flips a sentence/turn to the browser voice — most likely the availability-probe error path (V1) and/or a slow first byte tripping the 10 s watchdog (V2), driven by `t3.micro` CPU/RAM starvation (V3), possibly a flaky fail-closed Redis (V4) or Nginx buffering (V5). All intermittent and self-healing next turn/refresh — exactly your symptoms. *(CONFIRMED mechanisms; LIKELY the live mix.)*
2. **Is ElevenLabs itself the bottleneck?** **No at 3 users** (peak ~3 vs cap 15). It becomes the first bottleneck only near ~15 simultaneous sentence-synths fleet-wide (~a busy 70-student burst). *(CONFIRMED.)*
3. **Is the frontend state machine part of the problem?** The reducer is sound; the problem is its *inputs* — the probe-error→`voiceFailed` path (V1) and per-sentence fallback firing on infra-induced timeouts. Fix the probe logic and surface reasons. *(CONFIRMED.)*
4. **Are browser autoplay/playback failures part of the problem?** Possibly, on iOS typed-with-voice (V, §7). They must be counted separately (category C), not as ElevenLabs failures. *(LIKELY, secondary.)*
5. **Is backend concurrency part of the problem?** Not at 3 users. The design is correct (fleet semaphore, shed-on-full). The relevant backend risk is Redis fail-closed denials (V4) and the single-worker ceiling (V3). *(CONFIRMED.)*
6. **Are retries amplifying requests?** No provider amplification — retries run inside the held slot; worst-case 8 physical attempts/sentence but never higher concurrency (§8). *(CONFIRMED.)*
7. **Is Redis concurrency protection working correctly?** The algorithm is correct and honest, but **fail-closed means a Redis blip denies voice**, and on the current single-worker box Redis may be absent/disabled or flaky. **Verify `GET /api/health` `redis` status and the dashboard `concurrency_scope` now.** *(Correct by design; live state UNVERIFIED.)*
8. **Should we increase ElevenLabs concurrency right now?** **No.** It is not the constraint at your current scale and would not fix the 3-student symptom. Fix infra + V1/V2 first; validate the real plan number before any change. *(CONFIRMED.)*
9. **Should we raise OpenAI concurrency 20→40 right now?** **No.** OpenAI isn't the constraint at 3 (or even ~20) users, and on a single `t3.micro` raising the cap lets *more* work pile onto a box that's already starved — increasing thread pressure, memory use, timeouts, and *more* robotic fallbacks. Raise caps only *after* multi-worker + larger instance, guided by real load runs. Raising limits on an uncontrolled/undersized runtime makes things **worse**, not better. *(CONFIRMED reasoning.)*
10. **What must be fixed before attempting 65–70 users?** In order: **P0** V1 (probe-error handling), confirm Redis health/scope (V4), confirm streaming flag (V6); **P1** deploy planned 4-worker + Redis + ≥4 GB instance + Nginx no-buffering (V3/V5), Postgres off-box, ship client→server voice telemetry (§15); **P2** first-sentence priority + shared Redis audio cache + per-session fairness; **P3** faster model, optional circuit breaker, and only-if-needed multi-key/failover.
11. **What ElevenLabs/API capacity is actually required for 70 active students?** Because per-student TTS is sequential and duty-cycle is ~10%, **not 70 concurrent** — steady-state ~5–10 concurrent, bursts to ~15. A plan whose *real* concurrent-request allowance comfortably exceeds your tuned cap (start 15) is likely sufficient **with** first-sentence priority and duty-cycle spreading; confirm the real plan number and validate with a real-provider spike test. OpenAI: the 250k TPM / 3k RPM defaults are ample at a 20-slot cap. *(ESTIMATE; plan number UNVERIFIED.)*
12. **Can software scheduling allow 70 active students without 70 simultaneous TTS requests?** **Yes — and the code already does the hard part.** Sequential per-student pumping, a fleet cap with shed-on-full, first-sentence-first emission, and per-sentence fallback mean 70 students map to ~5–15 concurrent ElevenLabs requests, not 70. Add a first-sentence priority lane + shared cache and the scheduling is complete. *(CONFIRMED the mechanism exists; scale validation pending.)*

### Fix priority (gated)

- **P0 — before any further load testing (correctness/reliability only):** V1 (don't treat a probe error as "unavailable"); **verify Redis health + `concurrency_scope` + streaming flag on the live box** (one dashboard/health check each); confirm Nginx isn't buffering the two streaming routes.
- **P1 — before a 10-user real-provider test:** deploy planned infra (4 workers + Redis + ≥4 GB, Postgres off-box), Nginx streaming config, ship client→server voice telemetry (§15) so failures are attributable.
- **P2 — before a 30-user test:** first-sentence priority lane, shared Redis audio cache, per-session fairness, reconcile TTS-cap config/docs (B5), tune caps from measured data.
- **P3 — before the 65–70 production test:** faster model (`eleven_flash_v2_5`), optional circuit breaker, real-provider spike validation; multi-key/provider failover **only if** proven necessary.

---

### Appendix — Confirmed vs Likely vs Unverified

**CONFIRMED (read in source):** sequential per-student TTS; no `Promise.all`/parallel fan-out in the voice path; fleet TTS cap 15 with 5 s shed-to-browser; per-sentence isolation in the reducer; `voiceFailed` set only on probe result; probe error → `available:false` (not cached); 10 s progressive watchdog; ElevenLabs retries inside the held slot (no amplification); 409/502 error codes → one frontend retry then browser; single worker in `start.sh`; per-worker audio cache; Redis fail-closed in prod; short DB sessions around provider I/O; iOS unlock present.

**LIKELY (inference + symptoms):** V1/V2 as the live triggers; `t3.micro` starvation as the environmental cause; iOS typed-with-voice autoplay as a secondary cause.

**UNVERIFIED (not in repo — check on the live box):** production `.env` (streaming flag, `ENVIRONMENT`, `REDIS_REQUIRED_FOR_CONCURRENCY`, caps); whether Redis is provisioned/healthy; Nginx config (buffering/timeouts); real ElevenLabs plan concurrency; real OpenAI tier; whether the planned 4-worker migration has happened.

*No code, configuration, `.env`, AWS resource, or API key was modified in the course of this audit.*
