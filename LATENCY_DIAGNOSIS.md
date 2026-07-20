# PT AI Patient Simulator — Full Speech/Audio Pipeline Latency Diagnosis

Inspection only. No code changed, no files edited, no packages installed, no services restarted.
Times marked **[measured]** come from data in the repo (DB, config, code constants). Times marked
**[estimated]** are honest code-derived estimates plus published typical latencies, because the
backend was not running during this inspection and no dev-console `[patient-voice][timing]` /
`tts_timing` log capture exists in the repo (the instrumentation exists but only prints live in
DEV mode; nothing is persisted).

---

## 1. Executive conclusion

**The biggest latency source is the non-streaming OpenAI call: the app waits for the complete
strict-JSON `gpt-4o-mini` response (typically 1.5–4 s), then commits to the database, then returns
`patientText`, and only then starts the TTS request — every stage is strictly serial.**

---

## 2. Current end-to-end timing

```text
Student finishes question
→ final transcript              +500–1500 ms   (Web Speech API finalization, voice path only) [estimated]
→ OpenAI request                +5–20 ms       (frontend fetch + backend session/idempotency/history queries) [estimated]
→ patientText received          +1500–4000 ms  (gpt-4o-mini, full strict-JSON, non-streaming; validation + commit ~5–20 ms) [estimated]
→ ElevenLabs first byte         +400–1000 ms   (eleven_multilingual_v2 REST stream; +~150–300 ms TLS on first turn only) [estimated]
→ playback start                +50–250 ms     (first chunk append → updateend → remaining pause (usually 0) → play() → decode) [estimated]

Typical total, voice path:  ~2.5–6.5 s from end of speech to first audible patient word
Typical total, typed path:  ~2.0–5.0 s from pressing Send
```

The dev instrumentation that would confirm these numbers already exists:
frontend `devTiming()` in `src/services/patientVoiceService.ts` (`[patient-voice][timing] …`)
and backend `tts_timing …` logs in `app/api/voice.py` + `app/voice/elevenlabs_client.py`
(active because `.env` has `DEBUG=true`). There is **no** equivalent timing log around the
OpenAI call or the interview endpoint — that stage is the least instrumented and the largest.

---

## 3. Latency breakdown table

| # | Stage | File / function | Delay | Notes |
|---|-------|-----------------|-------|-------|
| 1 | Student finishes speaking | — | 0 | reference point |
| 2 | Final transcript | `speechRecognitionService.ts` `createRecognizer` (one-shot, `continuous=false`, `interimResults=true`) | 500–1500 ms [est] | Browser-internal endpointing; no app-side debounce exists |
| 3 | Frontend submits | `useVoiceConversation.ts` `handleFinalTranscript` → `runExchange` | ~0–5 ms | Immediate; no auto-submit debounce, no cooldown on this edge |
| 4 | Backend receives request | `api.ts` `request()` → `interviews.py` `send_message` | 1–10 ms [est] | localhost; CORS preflight cached (FastAPI CORSMiddleware `max_age` default 600 s) |
| 5 | Session/idempotency/history queries | `interview_service.send_student_message` → `SessionRepository.get`, `get_by_client_turn_id`, `list_turns` | 1–10 ms [est] | SQLite local file `ptai.db`; 3–4 point queries; `list_turns` loads the FULL transcript each turn |
| 6 | Case load + prompt build | `case_loader.load_case` (`@lru_cache`) + `prompt_builder.build_messages` | <1 ms | Cases cached in memory after first load; pure string work |
| 7–9 | OpenAI generation | `openai_client._do_generate` → `client.responses.create` (non-streaming) | **1500–4000 ms [est]** | `gpt-4o-mini`, strict JSON schema, `max_output_tokens=400` (env), full completion required |
| 10 | Response validation | `response_validator.validate_response` + `PatientReply.model_validate` | <5 ms | Local regex/pydantic |
| 11 | Turn commit | `interview_service` `append_turn` ×2 + `db.commit()` | 2–15 ms [est] | SQLite fsync on commit; happens BEFORE the HTTP response |
| 12 | Frontend receives patientText | `InterviewPage.performExchange` → 2× `addMessage` | 1–16 ms | Two setState batches; React renders before TTS kicks off |
| 13 | TTS request start | `patientVoiceService.speakPatientResponse` | +0–50 ms first turn: `getVoiceStatus` cached (`voiceStatusCache` Map) | Status fetched ONCE per case per page — not per turn |
| 14–15 | Voice profile/status resolution (backend) | `voice.py synthesize` → `load_voice_profile`, `map_speech_style`, `_resolve_approved_text` (1 DB get) | 1–5 ms | Cached case, one turn lookup |
| 16 | ElevenLabs first byte | `elevenlabs_client.stream_speech` (shared keep-alive `httpx.Client`) | **400–1000 ms [est]**; +150–300 ms first turn (TLS) | `eleven_multilingual_v2`, `mp3_44100_128`. Endpoint eagerly pulls the first chunk before sending headers |
| 17 | Frontend first chunk | `playProgressive` stream reader | +5–20 ms after backend first byte | Streamed through FastAPI `StreamingResponse` |
| 18 | SourceBuffer has enough | `canStartPlayback` — **exactly 1 chunk** | ~10–50 ms | Playback allowed after the FIRST `updateend` |
| 19 | `audio.play()` | `maybeStartPlayback` → `remainingPauseMs` | usually **0 ms extra** | `pauseBeforeMs` (default 150) overlaps with the network from request start; almost always fully consumed |
| 20 | `playing` fires | Chrome internal decode | 20–150 ms [est] | MP3 frame decode + output pipeline |
| 21 | Full download completes | during playback | overlapped | Truly progressive; not on the critical path |
| 22 | Speech ends | answer length dependent | 3–20 s | Carly's answers ~2× Camden's |

---

## 4. Typed versus voice comparison

Both paths converge on the identical `performExchange` → `sendStudentMessage` → `speakPatientResponse`
pipeline. The voice path adds latency **only before** the backend request:

- **Web Speech finalization: ~500–1500 ms [estimated].** `continuous=false`, and `onFinal` fires
  the instant the browser marks a result final. This is browser-internal silence detection; the
  app adds nothing on top.
- **No auto-submit debounce, no VAD gate on submission.** `handleFinalTranscript` submits
  immediately; `isUsableTranscript` is a synchronous regex. The VAD (`voiceActivityDetector.ts`)
  is used only for barge-in during patient speech, never on the submit path.
- **Not on the ask→answer path:** `COOLDOWN_MS = 800` and `INTERRUPT_SETTLE_MS = 300` delay when
  the mic starts listening *after* the patient finishes/is interrupted — they lengthen the gap
  between turns, not the time from question to answer. The recognizer restart loops (150/250 ms)
  likewise happen while idle-listening.
- Calibration (600 ms) happens once at conversation start only.

**Extra voice-path latency before the backend request starts: ≈ 0.5–1.5 s, entirely browser
speech-recognition finalization.** Typed input has effectively zero pre-request overhead.

---

## 5. OpenAI findings

- **Model:** `gpt-4o-mini` (`.env` overrides match the default). **API:** OpenAI Responses API
  (`client.responses.create`), **non-streaming**.
- **Strict JSON schema:** `PATIENT_REPLY_JSON_SCHEMA` with `strict: True`; requires
  `patient_text`, `used_fact_ids`, `response_type`, `supported`, and a full `speech` object
  (emotion/pace/energy/hesitation/pause_before_ms) or null — the metadata adds ~30–50 output
  tokens per turn and arrives at the END of generation.
- **Tokens:** `.env` sets `OPENAI_PATIENT_MAX_OUTPUT_TOKENS=400`. Timeout 30 s.
- **Retries:** two layers. (a) `fallback_manager.run_with_retry` → `openai_max_retries=1` = up to
  2 full attempts on any `PatientEngineError`, including a response that fails `response_validator`
  (character break/leakage). (b) The OpenAI SDK's own default `max_retries=2` on connection
  errors/429/5xx — the client is constructed without overriding it (`openai_client._get_client`),
  so a transient network error can silently retry inside one "attempt". No truncation retry for
  patient replies (`allow_truncation_retry` not passed by `generate()`).
- **Serial dependencies:** the complete JSON must parse and validate before anything else;
  student+patient turns are inserted and `db.commit()` runs **before** `TurnResponse` is returned;
  the frontend starts TTS only after receiving `patientText`.

Answers:

- **Is OpenAI the largest source?** Yes. At 1.5–4 s typical for a full 100–400-token structured
  completion it dwarfs every other stage.
- **Typical duration in observed runs:** not persisted anywhere; estimate 1.5–4 s (no timing log
  wraps the OpenAI call — this is the top instrumentation gap).
- **Waiting for the complete strict JSON?** Yes — `response.output_text` is only available when
  generation finishes, then `json.loads` + pydantic.
- **Could TTS begin before the full response, today?** No. `patient_text` is embedded inside the
  JSON envelope, the validator runs on the whole text, fact-disclosure accounting needs
  `used_fact_ids`, and the DB commit precedes the response. The architecture is strictly serial.
- **Is the response longer than necessary?** Mostly no — the prompt enforces 1–4 sentences and
  DB data confirms it (median 65–217 chars per case). Carly regularly produces 300–470-char
  answers, which costs both generation and speaking time.
- **Are retries silently doubling latency?** Occasionally, yes: a validation-failed generation
  triggers a full second OpenAI call (2× delay) with no log-visible turn-level marker other than
  the `Attempt 1/2 … failed` warning; SDK-internal retries are invisible in app logs entirely.

---

## 6. Prompt and context size inspection

- **Developer prompt template:** `app/prompts/patient_developer_prompt.txt` = 4,235 chars (~1.1k
  tokens), rebuilt every turn (template file re-read each turn via `load_template()` — trivially
  cheap, but note it is disk I/O per turn).
- **Persona block:** ~400–700 chars incl. `speech_behavior` lines (answer length, contractions,
  hesitation, vocabulary, directness, emotional-topic delivery).
- **Facts block:** topic-filtered subset via `fact_selector.select_facts` — NOT the whole case.
  Carly's full fact list would be 5,427 chars/28 facts (~193 chars avg); a typical topic match
  includes a handful, so ~0.5–2 KB.
- **Referral cases add** the hidden-context block (`_referral_block`) — a few hundred chars.
- **History:** last `MAX_HISTORY_TURNS = 12` turns only (`context_resolver.resolve_context`).
  The full transcript is *loaded from the DB* every turn (`list_turns`) but only 12 are sent, so
  the prompt reaches a plateau (~1.5–2.5 KB history) instead of growing without bound.
- **Rubric/assessment content:** none — verified that the interview prompt path imports nothing
  from `app/assessment` or `app/rubrics`. Clean separation.
- **Duplication:** the SPOKEN-LANGUAGE STYLE section of the template overlaps with per-case
  `speech_behavior` persona lines (both instruct answer length/contractions/hesitation). Harmless
  for quality, small token cost, negligible latency effect.

Total input estimate: **~6–10 KB ≈ 1.5–2.5k tokens** — modest. Input size is not the problem;
output-completion wait is. Nothing here meaningfully increases latency without helping quality.

---

## 7. Backend and database findings

- **Endpoints are sync `def`** (`interviews.send_message`, `voice.synthesize`) → FastAPI runs them
  in the threadpool, so the 1.5–4 s blocking OpenAI call and the blocking httpx ElevenLabs stream
  do **not** block the event loop. Correct pattern; no async/sync hazard found.
- **SQLite in dev** (`DATABASE_URL=sqlite:///./ptai.db` despite the Postgres default in config).
  Per turn: session `get`, idempotency lookup (indexed by client_turn_id? — it's a filtered query),
  full-transcript `list_turns`, 2× `next_turn_index`-style max queries inside `append_turn`, 2
  inserts, session update, one commit. All local-file point queries: **~2–15 ms total [estimated]**
  — measurable but ~0.5% of the turn. SQLite write locking is a non-issue single-user.
- **Commit ordering:** `db.commit()` completes **before** the frontend gets `patientText`, adding
  its few ms to the critical path. Generation-before-persist is intentional (no fake replies).
- **Case loading:** `@lru_cache` on `load_all_cases` — loaded and pydantic-validated once per
  process; not repeated per turn. Voice profile resolution rides the same cache.
- **Unnecessary per-turn work:** full transcript loaded when only the last 12 turns are used;
  prompt template file read from disk every turn; both are micro-costs.
- **First turn is slower:** case-file load + pydantic validation, OpenAI SDK client construction,
  ElevenLabs TCP+TLS handshake (shared keep-alive client, `keepalive_expiry=120 s` — an idle gap
  >2 min pays the handshake again).

---

## 8. ElevenLabs / frontend playback findings

**Backend voice path** (`app/api/voice.py`, `app/voice/elevenlabs_client.py`):

- **Model at runtime:** every case file sets `model_id: "eleven_multilingual_v2"` (also the
  settings default). This is ElevenLabs' highest-quality/highest-latency model — typical REST
  streaming first byte **~400–1000 ms** [estimated], versus ~75–200 ms class for flash/turbo.
- **Voice ID/status:** resolved from the lru-cached case per request (~µs). The `/voice/status`
  endpoint is hit once per case per page (frontend `voiceStatusCache`), not per turn.
- **Connection reuse:** module-level `httpx.Client` with keep-alive (4 keep-alive/8 max conns,
  120 s expiry) — the "new handshake per turn" problem is already fixed; only the first turn or
  a >2-minute idle gap pays TLS.
- **Output format:** `mp3_44100_128`. Fine for quality; ElevenLabs' lowest-startup options are
  lower-bitrate MP3 (e.g. `mp3_22050_32`) or raw PCM — startup difference is real but small
  relative to model first-byte time.
- **Eager first chunk:** `synthesize` pulls `next(upstream)` before returning the
  `StreamingResponse`, so browser TTFB ≈ ElevenLabs first byte. Good for error handling; means the
  frontend's "response headers" timing already contains ElevenLabs first-byte latency.
- **Cache:** in-memory LRU (24 entries) keyed on voice+model+text+settings. In a real interview
  almost every answer is unique, so it only helps repeated greetings and retries — marginal.
- **Retries:** none at the ElevenLabs layer (single attempt, 20 s timeout). Failure → generic
  502 → frontend browser-TTS fallback.

**Frontend playback** (`src/services/patientVoiceService.ts`, `voicePlaybackState.ts`):

- **Truly progressive:** MediaSource + `SourceBuffer('audio/mpeg')`; chunks append as they
  arrive; `canStartPlayback` requires exactly **one** appended chunk (`chunksReceived > 0` in
  phase `streaming`) — there is no multi-chunk or minimum-buffered-duration requirement.
- **`audio.play()` is called immediately when safe:** after the first `updateend`, minus any
  remaining pause. **`pauseBeforeMs` overlaps** with the network from TTS-request start
  (`remainingPauseMs`), so the default 150 ms (and most model-chosen values up to ~500 ms) is
  fully consumed by the 400–1000 ms first-byte wait → **usually 0 extra ms**. Only if the model
  emits a large pause (clamped ≤1500 ms) AND the network is fast would a real wait remain.
- **Chrome's `playing` event** adds decode/pipeline time (~20–150 ms) after `play()`.
- **Watchdog:** 10 s progressive-start timeout → fail over to browser TTS. Never fires on a
  healthy turn.
- **Blob fallback** (non-MediaSource browsers) waits for the FULL download before playing —
  Safari-class browsers get noticeably worse startup by design.
- **Playback begins before full download:** confirmed by code path (`STREAM_ENDED` handled
  independently of `PLAYBACK_STARTED`).

**Frontend pre-TTS serialization** (`InterviewPage.tsx`, `useVoiceConversation.ts`):

- After `patientText` arrives: 2× `addMessage` setState, one `dispatch`, then `speakPatientResponse`
  — one or two React render passes (~1–16 ms). No animation timers, no repeated API calls, no
  extra `await` chain of note. `getVoiceStatus` is awaited inside `speakPatientResponse` but is a
  Map hit after turn 1. The only theoretically parallelizable work: the TTS fetch could be fired
  before/along with the transcript render — savings ~ single-digit ms today.

---

## 9. Network inspection

Static analysis (services not running during inspection; no live DevTools capture):

- All app requests are `localhost:5173 → localhost:8000`: DNS ~0, connect ~0–1 ms, no TLS, no
  proxy. Vite does not proxy the API (frontend calls `API_BASE_URL` directly).
- **CORS:** cross-origin ports → JSON POSTs trigger an OPTIONS preflight; Starlette's
  CORSMiddleware default `max_age=600` caches it, so ~one extra round trip (~1–5 ms locally)
  every 10 minutes per endpoint, not per turn.
- **Keep-alive:** browser↔uvicorn connections are keep-alive by default; backend↔ElevenLabs uses
  the shared client (above); backend↔OpenAI uses the SDK's pooled client (created once).
- **Duplicated requests:** none found; idempotency (`clientTurnId`) protects against retry dupes.
- **First turn slower:** yes — voice-status fetch, ElevenLabs TLS handshake, OpenAI client
  warm-up, case-file load. Expect +300–800 ms on turn 1.
- **Upstream (real network) latency lives in** the OpenAI and ElevenLabs calls; everything local
  is single-digit ms.
- Production note (from VOICE_INTEGRATION.md): a buffering reverse proxy (nginx without
  `proxy_buffering off`) would silently destroy progressive playback — worth checking in any
  deployed environment, though not applicable to localhost.

---

## 10. Character-specific comparison (Carly, Sofia, Camden, Jayden)

All four use `eleven_multilingual_v2`, have real voice IDs, natural pause style, and identical
default `pause_before_ms` handling. Measured from `ptai.db` transcript data:

| Case | Patient answers (n) | Avg chars | Median | Max | Voice speed | Notable speech behavior |
|------|--------------------|-----------|--------|-----|-------------|------------------------|
| Camden | 20 | **83** | 65 | 186 | 1.02 | "1 short sentence", direct |
| Sofia | 36 | 136 | 116 | 338 | 0.90 | "1–2 sentences", downplays pain |
| Jayden | 16 | 150 | 122 | 421 | 0.98 | "1–3 sentences", searching for words |
| Carly | 35 | **203** | 217 | 470 | 0.94 | "1–3 sentences", high med-vocab, detailed; cancer topics "slower and quieter" |

- **Time-to-first-audio differs mainly through OpenAI output length:** non-streaming means every
  extra output token delays `patientText`. Carly's answers average ~2.5× Camden's → roughly
  0.5–1.5 s more generation wait on typical turns, plus slightly higher TTS first-byte for longer
  text.
- **Speaking duration** differs further via speed multipliers (Sofia 0.90 and Carly 0.94 speak
  more slowly per character than Camden 1.02).
- **Emotional pacing** (e.g. Carly on cancer, Jayden on lupus) maps to slower `pace` labels →
  `PACE_SPEED` down to 0.84× — lengthens playback, not startup. Model-chosen `pause_before_ms`
  above ~1000 ms could add startup delay that survives the overlap; the schema allows up to 1500.
- **Cache/referral logic:** no per-character cache or connection differences; referral cases add
  a slightly larger hidden prompt (input-side, minor).

**Yes: Carly (and to a lesser degree Jayden) feels slower primarily because her generated answers
are longer and more emotionally paced — not because of any per-character infrastructure
difference.**

---

## 11. Failure and retry latency

| Path | Config | Worst-case contribution |
|------|--------|------------------------|
| OpenAI app-level retry | `openai_max_retries=1` → 2 attempts (`fallback_manager`) | 2× generation (3–8 s), or 2×30 s timeout = **60 s** before PATIENT_RESPONSE_UNAVAILABLE |
| OpenAI SDK internal retry | SDK default `max_retries=2` (not overridden) | up to 3 transport attempts *inside each* app attempt on connection errors/429 — silent multiplier |
| ElevenLabs | no retry; 20 s timeout (connect 10 s) | one 20 s hang max, then 502 → browser TTS |
| Progressive watchdog | 10 s (`PROGRESSIVE_PLAYBACK_START_TIMEOUT_MS`) | a stalled MediaSource costs 10 s before browser-TTS fallback |
| Frontend fetch | no timeout of its own on `/synthesize` | bounded by backend behavior |
| Voice status | failure NOT cached → re-checked next turn | +1 fast request/turn during outages, negligible |
| Frontend/backend interview retry | none automatic — user-driven retry with same `clientTurnId` (idempotent replay, no regeneration) | good: replay is fast |

- **Healthy request worst case:** slow-but-successful OpenAI (~8–10 s inside the 30 s timeout) +
  ElevenLabs slow first byte (~2 s) ≈ **10–12 s**, no retries fired.
- **Failing request worst case:** 2×30 s OpenAI timeouts (**60 s**, more if SDK-internal retries
  extend each attempt) with the student staring at "processing"; or a good OpenAI turn followed by
  ElevenLabs 20 s timeout → browser TTS (audio ~20 s late but transcript already visible); or a
  MediaSource stall → 10 s watchdog → browser TTS.

---

## 12. Ranked root causes

| Rank | Stage | Exact file/function | Measured/estimated delay | Every turn? | Main cause? |
|------|-------|--------------------|--------------------------|-------------|-------------|
| 1 | OpenAI full non-streaming strict-JSON generation | `backend/app/patient_engine/openai_client.py` `_do_generate` (`client.responses.create`) | 1.5–4 s typical [est] | Yes | **YES** |
| 2 | Browser speech finalization (voice path only) | `src/services/speechRecognitionService.ts` `createRecognizer` | 0.5–1.5 s [est] | Voice turns | Second |
| 3 | ElevenLabs first byte on `eleven_multilingual_v2` | `backend/app/voice/elevenlabs_client.py` `stream_speech` | 0.4–1.0 s [est] | Yes | Third |
| 4 | Strictly serial architecture (LLM→validate→commit→respond→TTS; no overlap) | `interview_service.send_student_message` + `InterviewPage.performExchange` | forces 1+3 to add, ~0 overlap | Yes | Structural amplifier |
| 5 | Retry behavior (app 2× + SDK-internal) | `fallback_manager.run_with_retry`; OpenAI SDK defaults | 2× stage-1 on affected turns | Occasional | On bad turns |
| 6 | Answer length (esp. Carly) | case `speech_behavior` + prompt rule 5 | +0.5–1.5 s generation on long answers | Case-dependent | Contributor |
| 7 | First-turn connection setup (ElevenLabs TLS, OpenAI client, status fetch, case load) | `elevenlabs_client.get_http_client`, `patientVoiceService.getVoiceStatus` | +0.3–0.8 s | First turn / after 2 min idle | Minor |
| 8 | MediaSource buffering + decode before `playing` | `patientVoiceService.playProgressive` (1-chunk threshold) | 50–250 ms [est] | Yes | Minor |
| 9 | DB queries + commit before response | `interview_service` + `transcript_repository` (SQLite) | 2–15 ms [est] | Yes | Negligible |
| 10 | Frontend serialization (setState → TTS start) | `InterviewPage.performExchange` → `speakPatientResponse` | 1–16 ms | Yes | Negligible |
| 11 | `pauseBeforeMs` | `voicePlaybackState.remainingPauseMs` (overlapped from request start) | ~0 ms typical (≤1350 ms pathological) | Yes | Already solved |
| 12 | Network setup (localhost, CORS preflight cached) | `api.ts` / CORSMiddleware | ~1–5 ms per 10 min | No | Negligible |

---

## 13. Recommendations (NOT applied)

### 11. Low-risk quick wins

1. **Instrument the OpenAI stage** — the one big stage with no timing log (mirror the existing
   `tts_timing` pattern around `generate_patient_response` and the endpoint). Gain: certainty, not
   speed. Difficulty: trivial. No behavioral risk.
2. **Fire the TTS request without awaiting transcript render** (start `speakPatientResponse`
   before/parallel to the `addMessage` state updates). Gain: ~5–20 ms. Trivial; no case-control,
   interruption, or transcript risk.
3. **Pin the OpenAI SDK's internal `max_retries` explicitly** (e.g. to 0–1) so app-level retry is
   the only multiplier and worst-case turn time is predictable. Gain: bounded tail latency. Low
   risk; slightly fewer silent recoveries.
4. **Trim `max_output_tokens` for patient replies** (400 → ~220–250; median answers are ≤220
   chars) and/or tighten Carly's `average_answer_length`. Gain: 100–400 ms on long turns +
   shorter speech. Small risk: occasional truncation → validation failure → retry; test first.
5. **Cache the prompt template in memory** (`load_template()` re-reads the file each turn) and
   have `list_turns` fetch only the last N turns. Gain: a few ms. No risk.
6. **Reduce the progressive watchdog / ElevenLabs timeout tails** (e.g. 10 s → 6 s, 20 s → 10 s)
   so failures fall back sooner. Gain: failure-path only. Low risk.
7. **Consider caching voice-status failures briefly** (currently refetched every turn during an
   outage). Negligible gain; tidiness.

### 12. Medium-risk improvements

1. **Faster ElevenLabs model** (`eleven_turbo_v2_5` or `eleven_flash_v2_5`) per case or globally.
   Gain: ~0.3–0.8 s to first audio every turn. Risk: emotional nuance/voice quality (Carly's
   "slower and quieter" delivery is where multilingual_v2 earns its cost); cheaper per character.
   Requires listening tests per character. No transcript/assessment change.
2. **Faster OpenAI model for patient dialogue** (e.g. the account's fastest small model). Gain:
   potentially 0.5–2 s. Risk: fact discipline, disclosure-rule adherence, referral hidden-concern
   behavior — needs eval against the existing validator before any switch.
3. **Lower-startup audio format** (lower-bitrate MP3 or PCM with a Web Audio path). Gain:
   ~50–150 ms. Risk: quality/decoder-path changes; MediaSource type support differences.
4. **Shrink the history window** (12 → 8 turns) and dedupe the spoken-style instructions. Gain:
   marginal (input tokens are not the bottleneck). Low quality risk.
5. **Async DB path / commit-after-respond** (return `patientText`, commit in background). Gain:
   2–15 ms. Risk: transcript durability ordering — probably not worth it.

### 13. Architectural changes

1. **Stream the OpenAI response** (streaming Responses API), showing/synthesizing text as it
   arrives. Gain: 1–3 s perceived. Requires abandoning or restructuring the strict-JSON envelope
   (see §14). High impact on validation, disclosure accounting, and retry semantics.
2. **Sentence-level TTS pipelining** (first sentence → ElevenLabs while generation continues).
   Gain: time-to-first-audio ≈ first-sentence time (~0.7–1.5 s total). Same JSON obstacle +
   interruption/transcript consistency work.
3. **Two-call split:** a fast plain-text `patient_text` stream + a parallel/after-the-fact
   metadata call (speech labels, fact IDs). Keeps case control server-side; doubles OpenAI calls.
4. **ElevenLabs WebSocket streaming input** — pairs naturally with (1)/(2); removes per-turn HTTP
   setup and lets TTS start on partial text. Moderate complexity.
5. **OpenAI Realtime / full-duplex audio** — eliminates the Web Speech finalization delay AND
   TTS chaining, but replaces the entire carefully-built control stack (validator, disclosure
   manager, transcript authority, barge-in logic). Highest risk to medical case control.
6. **Server-side conversation orchestrator** (backend drives STT→LLM→TTS over one socket) —
   the long-term shape if (1)–(4) are pursued; big rewrite.

Every architectural option risks: (a) response validation now runs on partial text — a
character-break could be *heard* before it is caught; (b) transcript authority (`/synthesize`
verifies stored turn text) must be reworked for partial audio; (c) interruption/echo logic
currently assumes one atomic clip per turn.

---

## 14. Sentence-level pipelining feasibility

```text
OpenAI produces first complete sentence → ElevenLabs starts speaking it → OpenAI continues
```

- **The current strict JSON schema prevents it.** `patient_text` is a field inside a JSON object
  that also carries `used_fact_ids`/`response_type`/`speech`; nothing is parseable until the
  object closes. Any pipelining design must move to streamed plain text (with a trailing or
  parallel metadata channel) or incremental JSON parsing of the `patient_text` string value.
- **Incomplete-medical-answer risk:** real. `response_validator` (character break/leakage) runs
  on the full text today; with pipelining, the first sentence is already audible before the last
  sentence is validated. Mitigation: per-sentence validation, accepting weaker guarantees.
- **Speech metadata timing:** `speech` labels arrive at the END of generation but are needed at
  TTS start. Would need to move `speech` first in the schema (with streamed parsing), use the
  case's default profile for sentence 1, or split the calls.
- **Transcript consistency:** solvable — commit the full text once generation completes; the
  `/synthesize` stored-turn verification would need a variant for partial-sentence synthesis.
- **Interruptions get harder:** today cancel = abort one fetch + one audio element. Pipelined,
  it means cancelling an OpenAI stream + N queued TTS clips + partial-turn transcript decisions.
  The existing generation-guard design (`voicePlaybackState`) extends to this, but it is real work.
- **Worth considering?** Yes — it is the only approach that attacks the full serial sum
  (OpenAI + ElevenLabs ≈ 2–5 s) rather than one term, and it can cut perceived latency to
  ~1–1.5 s. But only after the cheap wins and model evaluation, given the safety trade-offs.

---

## 15. Model options (conceptual comparison — nothing switched)

| Option | Time to first token/byte | Voice/output quality | Emotional quality | Cost | Medical reliability |
|--------|--------------------------|----------------------|-------------------|------|--------------------|
| `gpt-4o-mini` (current) | full completion 1.5–4 s (non-streaming) | good instruction-following | n/a | low | proven against this validator |
| Faster/newer small OpenAI models (as available to the account) | similar per-token; wins come mainly from *streaming*, not model swap | varies | n/a | similar/lower | must re-verify fact discipline + strict-schema support |
| `eleven_multilingual_v2` (current) | ~400–1000 ms first byte [est] | highest | best (matters for Carly/Jayden emotional turns) | highest | n/a |
| `eleven_turbo_v2_5` | ~250–300 ms class [est] | high | good | ~50% cheaper | n/a |
| `eleven_flash_v2_5` | ~75–150 ms class [est] | good | flattest of the three | ~50% cheaper | n/a |

(Per-model first-byte figures are from ElevenLabs' published positioning; verify with a live A/B
before deciding.) The biggest OpenAI-side gain available *without* changing models is switching
the same model from non-streaming to streaming.

---

## 16. Best recommended plan (phased — not implemented)

- **Phase 1 — measure + low-risk (days):** add OpenAI-stage timing logs; capture 20–30 real turns
  per character with the existing `[patient-voice][timing]`/`tts_timing` instrumentation; pin SDK
  retries; trim patient max-output-tokens; start TTS without awaiting the transcript render.
  Expected: hard numbers + ~0.2–0.5 s shaved, tails bounded.
- **Phase 2 — model evaluation (1–2 weeks):** A/B `eleven_turbo_v2_5`/`flash_v2_5` per character
  against multilingual_v2 (listening tests on emotional turns); evaluate a faster OpenAI model
  against the response validator and disclosure tests. Expected: 0.5–1.5 s if quality holds.
- **Phase 3 — only if still needed:** streamed OpenAI text with restructured output +
  sentence-level ElevenLabs pipelining (WebSocket input), keeping validation and transcript
  authority server-side. Expected: perceived latency ~1–1.5 s; largest engineering and
  case-control risk, so it comes last.

---

## Final answer

**The current delay is mainly caused by the non-streaming OpenAI generation — the app waits for
the complete strict-JSON `gpt-4o-mini` response before anything else can start — followed by
browser speech-recognition finalization on the voice path and the `eleven_multilingual_v2`
first-byte latency, all stacked strictly serially.**

*No project files were modified. All recommendations await your review and approval.*
