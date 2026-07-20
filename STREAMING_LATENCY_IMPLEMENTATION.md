# Streaming Patient Response Pipeline — Implementation Report

Feature-flagged, reversible low-latency pipeline. **Default state: OFF** — the
application behaves exactly as before until you enable it (see §15).

---

## 1. Architecture before (stable path — still present, still the default)

```text
Student question
→ FastAPI POST /api/interviews/{id}/messages
→ OpenAI generates FULL strict-JSON response (non-streaming)
→ full-response validation (response_validator)
→ student + patient turns committed
→ frontend receives patientText + speech labels
→ ElevenLabs synthesizes the whole answer (progressive MediaSource playback)
→ transcript and assessment continue
```

Every stage is serial; typical first audible word ≈ 2.5–6.5 s
(see LATENCY_DIAGNOSIS.md).

## 2. Architecture after (streaming path, behind flags)

```text
Student question
→ FastAPI POST /api/interviews/{id}/messages/stream   (SSE)
→ ONE OpenAI streaming request (plain text first, ===META=== JSON tail)
→ SentenceAccumulator detects complete sentences incrementally
→ CUMULATIVE safety validation per sentence (existing validator + leak checks)
→ approved sentence emitted as SSE "sentence" event
→ frontend queues sentence TTS (existing /api/voice/synthesize, keep-alive)
→ patient audio starts on sentence 1 while OpenAI keeps generating
→ later approved sentences buffer and play strictly in order
→ metadata tail parsed after text completes (never blocks speech)
→ ONE authoritative patient turn committed (short transaction)
→ SSE "final" event; transcript and assessment use the saved text
```

## 3. Files changed

**Backend — new:** `app/patient_engine/sentence_stream.py`,
`app/patient_engine/streaming_engine.py`,
`app/services/interview_stream_service.py`, `tests/test_streaming.py`.
**Backend — modified:** `app/core/config.py` (flags),
`app/core/exceptions.py` (StreamingDisabledError),
`app/database/connection.py` (get_db_factory),
`app/api/interviews.py` (stream route + /interviews/config),
`app/api/voice.py` (correlation-id in timing logs),
`app/schemas/interview_schema.py` (InterviewConfigOut),
`app/schemas/voice_schema.py` (correlation_id field),
`app/patient_engine/openai_client.py` (stream_text),
`app/patient_engine/prompt_builder.py` (build_streaming_messages),
`app/patient_engine/response_validator.py` (validate_stream_text),
`app/services/interview_service.py` (OpenAI-stage timing log only).
**Frontend — new:** `src/services/streamingQueueState.ts` (pure, unit-tested),
`src/services/patientStreamService.ts`, `src/services/streamRegistry.ts`,
`scripts/test-streaming-queue.mjs`.
**Frontend — modified:** `src/services/api.ts` (fetchInterviewConfig),
`src/services/patientVoiceService.ts` (cancel also cancels active stream),
`src/types/interview.ts` (PatientExchange.streaming),
`src/state/AppContext.tsx` (setMessages accepts functional updates),
`src/pages/InterviewPage.tsx` (streaming branch + fallback),
`src/hooks/useVoiceConversation.ts` (streaming branch in beginSpeaking),
`package.json` (test:voice includes the new suite).

Nothing in the assessment engine, case files, rubrics, echo prevention, or
visual design was changed.

## 4. OpenAI streaming design

ONE `client.responses.create(stream=True)` request per patient turn (verified
by test). The streaming prompt reuses the full existing developer prompt
(persona, facts, disclosure rules, referral hidden-context) plus an output
override: plain spoken text first, then a `===META===` line, then one compact
JSON object with `used_fact_ids`, `response_type`, `supported`, `speech`.
The delimiter is detected even when split across deltas (held-back tail
window). Never one request per sentence.

## 5. Sentence validation (the safety gate)

A sentence is approved only if the **entire cumulative text including it**
passes `validate_stream_text`, which runs the UNCHANGED existing
`validate_response` (character breaks, forbidden phrases, other-case names,
length cap) plus streaming-specific checks: the metadata delimiter, internal
field names ("used_fact_ids", "hidden context", …), and the case's literal
fact IDs must never be spoken. Cumulative validation also catches forbidden
phrases spanning a sentence boundary. Disclosure eligibility is enforced the
same way as before: facts are filtered by the existing DisclosureManager
BEFORE prompting, and only eligible fact ids from the metadata tail are
accounted. Rejections: first sentence rejected → nothing spoken, error event,
frontend falls back to the stable path; later sentence rejected → it is never
spoken, the already-approved text is kept and committed
(`validation_status=valid_truncated_stream`). Malformed metadata degrades
safely: NO fact ids marked disclosed (conservative), default response_type,
case-default speech labels.

## 6. ElevenLabs streaming design — HTTP per sentence (not WebSocket)

Chosen: per-sentence HTTP through the existing `/api/voice/synthesize`
endpoint over the existing module-level keep-alive `httpx.Client` (no new TLS
handshake per sentence). Why not WebSocket streaming input: the HTTP path
reuses the tested voice-profile mapping, clamping, caching, transcript-
verification and fallback code with zero new session management (reconnects,
late-audio suppression, server-side socket lifetime), and with keep-alive its
first-byte cost is the same model-side wait that dominates either way.
`ELEVENLABS_STREAMING_INPUT_ENABLED` is reserved (default false) if WebSocket
input is wanted later. Voice ID, stability, similarity, style, speaker boost,
speed, fallback rate, and case-level model overrides are untouched.

## 7. Frontend audio queue

`streamingQueueState.ts` is a pure reducer (unit-tested in Node) enforcing:
strict in-order playback, no overlapping clips, exactly-once per sentence
index, terminal CANCEL absorbing all late events, and THREE distinct states:
generation done ≠ TTS done ≠ playback done — the speaking indicator and the
voice state machine settle only at playback done. Sentence 1 audio is
requested the moment the sentence event arrives (before the voice loop even
transitions), sentence 2 buffers while 1 plays. `pauseBeforeMs` applies only
before sentence 1 and is overlapped with network time (same rule as before).

## 8. Interruption behavior

`cancelPatientSpeech()` remains the single entry point (used by the Interrupt
button, barge-in VAD, typed barge-in, route changes, unmount). For a
streaming turn it: aborts the SSE fetch → uvicorn raises GeneratorExit in the
backend generator → the OpenAI stream is closed (engine finally) → the
backend commits exactly the sentences already emitted; aborts any in-flight
sentence TTS fetch; stops the active audio element; clears queued blobs;
cancels browser TTS; dispatches terminal CANCEL (late chunks/events ignored);
settles every promise exactly once. Covered phases: before the first delta,
during first-sentence generation, after approval but before audio, during
sentence-1 playback, while later sentences generate/queue, near the end, and
during browser-TTS fallback. The existing settle delay + mic restart flow is
unchanged (`INTERRUPT_SETTLE_MS`, cooldown).

## 9. Transcript behavior (documented decision — Option A)

Normal completion: ONE patient turn with the full approved text (exactly what
was displayed and spoken). Interruption after ≥1 sentence: the emitted
sentences are committed as the patient turn with
`validation_status="interrupted"` — the transcript and assessment match what
the student actually saw/heard. Interruption or failure before any sentence:
nothing is persisted; the student keeps the question (identical to the stable
path's failure semantics). Note: the stable path's behavior is unchanged —
there, interruption during playback still stores the full generated text
(that has always been its rule).

## 10. Fallback behavior

```text
Streaming OpenAI + sentence TTS
→ any failure BEFORE a sentence is spoken: stable atomic path, SAME clientTurnId
→ ElevenLabs failure mid-turn: browser TTS speaks remaining sentences in order
→ autoplay rejection: browser TTS for that sentence
→ missing voice ID / voice unavailable: browser TTS per sentence from the start
→ no TTS at all: transcript-only, interview continues
```

Covered triggers: streaming API failure, first-sentence validation failure,
parser producing nothing, OpenAI failure, ElevenLabs failure, autoplay
rejection, unsupported browser (config fetch failure → streaming off), user
cancellation, missing voice ID, missing API key (voice status unavailable),
flag off (409 streaming_disabled → stable path). Idempotent `clientTurnId`
guarantees the fallback can never duplicate transcript rows or regenerate a
committed answer; the queue reducer guarantees no replayed sentence 1, no two
simultaneous voices, no restart of a cancelled answer.

## 11. Retry policy and cost behavior

Streaming attempt: **no application-level retry** (a half-spoken answer must
never be retried); OpenAI SDK connection retries can only occur before the
first token. On pre-speech failure the ONE fallback is the stable path, which
keeps its existing policy (2 app attempts, 30 s timeout each). Worst-case
wait before the student sees an error ≈ streaming failure detection (≤30 s
timeout, typically <2 s) + stable path worst case (2×30 s) — unchanged tail
vs. today except the added, typically tiny, streaming attempt.
Cost per turn: 1 OpenAI streamed request (input/output tokens logged in DEBUG
via `stream_timing mark=generation_complete`), each sentence sent to
ElevenLabs exactly once (per-sentence chars sum = final text chars; logged),
TTS session count = number of sentences over ONE keep-alive connection.
Duplicate `clientTurnId` replays send nothing to OpenAI or ElevenLabs
(tested).

## 12. Latency results — NOT YET MEASURED (honest)

This environment has no browser, microphone, or audio output, so real
first-audio numbers cannot be produced here and none are invented. The full
instrumentation is in place on both sides (`[patient-stream][timing]` in the
browser console; `stream_timing` / `interview_timing` / `tts_timing` with the
clientTurnId correlation id in the backend log, DEBUG=true). Expected
mechanism-based improvement: first audio moves from "full answer + TTS first
byte" to "first sentence + TTS first byte" — with `gpt-4o-mini` first-sentence
time (~0.5–1.2 s) + `eleven_turbo_v2_5` first byte (~0.3–0.8 s), the 1–2 s
target is plausible but MUST be verified with the measurement protocol below.

| Case   | Before first audio | After first audio | Improvement |
| ------ | -----------------: | ----------------: | ----------: |
| Carly  |     (measure)      |     (measure)     |             |
| Sofia  |     (measure)      |     (measure)     |             |
| Camden |     (measure)      |     (measure)     |             |
| Jayden |     (measure)      |     (measure)     |             |

Protocol: for each case ask the same 5 questions (greeting, pain, follow-up,
emotional, long-answer) once with `OPENAI_PATIENT_STREAMING_ENABLED=false`
and once with `true`; read "question submitted -> first audible patient word"
from the dev console (streaming) and "patient response → playback start"
(stable), 3 runs each, report medians.

## 13. Test results (run in this session, Linux sandbox)

- Backend: `python3 -m pytest` → **136 passed, 0 failed** (110 baseline + 26
  new in `tests/test_streaming.py`: accumulator edge cases, engine safety
  gates, one-request-per-turn, SSE endpoint flow, idempotent replay,
  failure-before-speech persists nothing, disconnect partial commit,
  disabled-flag 409, case mismatch).
- Frontend: `npm run test:voice` → **58 tests, 49 pass, 0 fail** (9 “not ok”
  entries are pre-existing TODO-marked progressive-playback tests, identical
  to the pre-change baseline). 12 new streaming-queue tests all pass.
- `tsc -b` clean; `vite build` succeeds (verified on a clean Linux install);
  `oxlint`: 0 errors, 2 pre-existing warnings in untouched files.
- NOT tested here (needs your machine): real browser audio, microphone
  interruption, live OpenAI/ElevenLabs latency. Audio-element behavior beyond
  the pure queue rules is not claimed as tested.

## 14. Remaining risks

- **Partial-sentence exposure:** per-sentence validation is inherently weaker
  than whole-answer validation — a sentence that is fine alone but wrong in
  the context of an unseen LATER sentence will already have been spoken.
  Cumulative validation and unchanged fact eligibility limit this.
- **Metadata tail reliability:** if the model omits/garbles `===META===`,
  fact-disclosure accounting for that turn is skipped (conservative: nothing
  marked disclosed → the patient may re-volunteer a fact later as if new).
- **Inter-sentence audio gaps:** sentence-level HTTP TTS can leave small gaps
  between sentences (each clip fully buffers before playing). ElevenLabs
  WebSocket input would smooth this if it matters in practice.
- **Voice continuity:** mid-turn ElevenLabs failure switches to the browser
  voice mid-answer (documented fallback; jarring but transparent).
- **Toggling “Speak patient replies” off mid-stream** cancels the whole
  streamed turn (partial commit), not just the audio.
- **End Interview during a streamed turn:** the backend's partial commit races
  the transcript verification fetch by a few ms; the flush→verify order makes
  this unlikely but not impossible.
- **Proxy buffering in production:** SSE + streamed audio require
  `proxy_buffering off` (the endpoint sends `X-Accel-Buffering: no`).
- **Browser support:** SSE-over-fetch needs ReadableStream (all evergreen
  browsers); older Safari falls back automatically (config fetch/stream
  failure → stable path).
- **Interrupted-transcript semantics changed for streamed turns** (Option A
  above) — intentional and documented, but different from the stable path.

## 15. Manual steps — enable, test, disable

Enable (backend `.env`):
```env
OPENAI_PATIENT_STREAMING_ENABLED=true
PATIENT_SENTENCE_PIPELINING_ENABLED=true
DEBUG=true            # keep on while measuring
```
Restart uvicorn. The frontend picks the flags up automatically via
`GET /api/interviews/config` (hard-refresh the browser tab).

Verify: open dev console → ask a question → look for
`[patient-stream][timing] question submitted -> first audible patient word`.
Backend log shows `stream_timing mark=...` lines with the same clientTurnId.
Test one interview per case, one mic interruption during playback, and one
fallback (temporarily set a wrong ELEVENLABS_API_KEY → browser voice).

Instant rollback (no code changes): set
`OPENAI_PATIENT_STREAMING_ENABLED=false`, restart uvicorn. Full rollback of
all code: restore from `PT-AI-backup-pre-streaming-2026-07-16.tar.gz` in the
project folder.
