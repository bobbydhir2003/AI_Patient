# ElevenLabs Patient Voice Integration

Every patient character can speak with a realistic, unique ElevenLabs voice.
The architecture is unchanged: **OpenAI is the patient brain, ElevenLabs is
only the patient voice, React/FastAPI is the conversation controller.**

```text
Student microphone
→ Browser speech recognition (unchanged)
→ FastAPI /api/interviews/{sessionId}/messages
→ OpenAI patient engine (unchanged; now also returns delivery labels)
→ Approved patientText + controlled speech metadata
→ FastAPI /api/voice/synthesize → ElevenLabs (streamed)
→ React audio playback (Blob)
→ Transcript and assessment (unchanged)
```

## 1. Setting the API key

Add your key to `backend/.env` (never commit it, never create a `VITE_ELEVENLABS_*` variable):

```env
ELEVENLABS_API_KEY=your-key-here
ELEVENLABS_ENABLED=true
ELEVENLABS_DEFAULT_MODEL=eleven_multilingual_v2
ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
```

The key lives only on the FastAPI backend. The browser calls FastAPI; FastAPI
calls ElevenLabs. The key is never logged and never appears in any response.

## 2. Adding each character's voice ID

1. Open the ElevenLabs dashboard → Voices, pick (or design) a voice per character.
2. Copy the voice ID (the alphanumeric ID, not the name).
3. Paste it into `voice_profile.voice_id` in the case file under `backend/app/cases/`.
4. Restart the backend.

| Character | Case ID          | Case file                | Voice ID    | Voice behavior                          | Status         |
| --------- | ---------------- | ------------------------ | ----------- | --------------------------------------- | -------------- |
| Carly     | carly            | `carly.json`             | placeholder | Warm, thoughtful, clinically literate   | Needs voice ID |
| Sofia     | sofia            | `sofia.json`             | placeholder | Gentle, cautious, reserved teen         | Needs voice ID |
| Camden    | camden           | `camden.json`            | placeholder | Casual, direct, energetic preschooler   | Needs voice ID |
| Jayden    | jayden           | `jayden.json`            | placeholder | Warm, conversational, upbeat            | Needs voice ID |
| Jordan    | referral_case_01 | `referral_case_01.json`  | placeholder | Guarded, short answers                  | Needs voice ID |
| Eleanor   | referral_case_02 | `referral_case_02.json`  | placeholder | Polite, understated, worried underneath | Needs voice ID |
| Marcus    | referral_case_03 | `referral_case_03.json`  | placeholder | Low energy, slower, deflects with humor | Needs voice ID |
| Priya     | referral_case_04 | `referral_case_04.json`  | placeholder | Quiet, tired, composed and flat         | Needs voice ID |

Placeholders look like `PASTE_CARLY_VOICE_ID_HERE`; the backend treats them as
"not configured" and the app automatically uses browser TTS for that case.
Voices are configuration only — do not select voices that stereotype race,
disability, nationality, or medical condition.

## 3. How voice profiles work

Each case file may contain an optional `voice_profile` block:

```json
"voice_profile": {
  "provider": "elevenlabs",
  "voice_id": "PASTE_CARLY_VOICE_ID_HERE",
  "model_id": "eleven_multilingual_v2",
  "speed": 0.94,
  "stability": 0.5,
  "similarity_boost": 0.78,
  "style": 0.1,
  "speaker_boost": true,
  "default_emotion": "warm",
  "pause_style": "natural",
  "fallback_rate": 0.94,
  "enabled": true
}
```

Missing fields get safe defaults; missing/placeholder `voice_id` (or
`"enabled": false`) means browser TTS is used for that case. Voice IDs are
never exposed to the frontend and can never be supplied by it. All numeric
values are re-clamped server-side (`app/voice/speech_style_mapper.py`), so a
mistyped profile cannot send unsafe values to ElevenLabs.

## 4. How controlled speech metadata works

The OpenAI patient engine returns, alongside the reply, controlled delivery
labels (never raw numbers):

- `emotion`: neutral | warm | relieved | worried | anxious | frustrated | guarded | sad | tearful | confused
- `pace`: very_slow | slow | normal | fast
- `energy`: low | normal | high
- `hesitation`: none | mild | moderate
- `pauseBeforeMs`: 0–1500

The backend mapper merges these with the case profile and clamps everything
(speed 0.7–1.2, stability 0.15–0.9, similarity 0.3–1.0, style 0.0–0.6). Invalid
labels fall back to defaults. **Metadata shapes only how the approved text
sounds — it never changes what the patient reveals**, and it is not stored in
the transcript or used by the assessment.

## 5. Fallback behavior

Per response, in order:

1. ElevenLabs available for the case (`GET /api/voice/status/{caseId}`) → synthesize via backend, play as Blob audio.
2. ElevenLabs disabled, unconfigured, out of credits, or the request/playback fails → browser `speechSynthesis` speaks the same approved text (at the profile's `fallback_rate`).
3. Both providers fail → the reply still appears in the transcript and the interview continues; assessment is unaffected.

A TTS failure never fails the exchange, never duplicates a transcript entry,
and never changes assessment scoring.

## 6. How interruption cancellation works

`cancelPatientSpeech()` (frontend, `src/services/patientVoiceService.ts`):

1. Invalidates every in-flight playback generation (late responses are dropped, so cancelled audio can never resume).
2. Aborts any pending `/api/voice/synthesize` fetch via `AbortController`.
3. Stops the active audio element (`pause()`, reset `currentTime`, detach `src`, `load()`), revokes the Blob URL, and clears handlers.
4. Cancels browser `speechSynthesis` (fallback path only).
5. Resolves the pending speak promise so the state machine proceeds.

The existing voice state machine is unchanged: interruption still goes
SPEAKING → INTERRUPTING → (settle delay) → LISTENING, with the same cooldowns,
echo-cancellation constraints, and the interruption lock. Recognition stays off
while the patient speaks, and the VAD barge-in arms only at actual playback
start, so patient audio is not transcribed as student speech.

## 7. Streaming and playback model

**Progressive playback (end-to-end streaming).** The backend streams audio
from ElevenLabs (`.../stream` endpoint) through FastAPI's `StreamingResponse`
with keep-alive connection reuse (one shared `httpx` client, closed on
shutdown). The browser reads `response.body` with a stream reader and appends
chunks into a MediaSource SourceBuffer (`audio/mpeg`); **playback starts as
soon as the first chunk is buffered**, while the rest keeps downloading.

The natural `pauseBeforeMs` is **overlapped** with the request: the pause
timer starts when the TTS request starts, and only the remaining portion (if
any) is waited before playback.

Fallback chain: if MediaSource/`audio/mpeg` is unsupported, the same response
is played as a Blob (full buffering, ElevenLabs voice kept); if ElevenLabs or
streaming playback fails, browser TTS speaks the same approved text.

Development builds log a per-turn latency breakdown to the console
(`[patient-voice][timing] …`: request start, headers, first chunk, playback
start, download complete, playback end) and the backend logs upstream timing
(`tts_timing …`) when `DEBUG=true`. Nothing sensitive is logged.

## 8. Disabling ElevenLabs locally / testing without credits

- `ELEVENLABS_ENABLED=false` in `backend/.env` disables it globally (browser TTS everywhere).
- `"enabled": false` in one case's `voice_profile` disables just that case.
- Leaving `ELEVENLABS_API_KEY` empty also disables it safely.
- A bounded in-memory cache means repeating the same reply (voice + text + settings) does not re-consume credits.
- All tests (`pytest backend`, `npm run test:voice`) run with the ElevenLabs boundary faked; no credits are used.

## 9. Configured model & endpoint validation

- Model: `ELEVENLABS_DEFAULT_MODEL` (default `eleven_multilingual_v2`); a case can override via `voice_profile.model_id`.
- `POST /api/voice/synthesize` validates the case ID, rejects text over `ELEVENLABS_MAX_TEXT_CHARS`, ignores any frontend-supplied voice ID, and — when `sessionId`/`turnId` are provided (the normal path) — synthesizes the **saved patient turn verbatim**, so the endpoint cannot voice arbitrary text.
- Errors are safe and generic (`voice_unavailable` 409, `voice_synthesis_failed` 502); no upstream details or key material.

## 10. Browser / deployment considerations

- Microphone (and therefore voice mode) requires HTTPS in production (localhost is exempt).
- CORS: the API exposes the `X-Pause-Before-Ms` header (configured in `app/main.py`); keep `CORS_ORIGINS` up to date.
- If your hosting proxy buffers responses (e.g. nginx without `proxy_buffering off`), progressive playback degrades gracefully: the audio arrives as one late burst and playback starts then — still correct, just slower. Disable proxy buffering for `/api/voice/synthesize` to keep the latency benefit.
- Ensure the proxy allows response bodies of a few hundred KB (MP3 clips) and request timeouts ≥ `ELEVENLABS_TIMEOUT_SECONDS`.
- Leaving/refreshing the interview page cancels pending TTS and releases audio, the microphone, and Blob URLs (existing unmount cleanup).

## Logging

Development logs include: selected case ID, provider, TTS request start/
completion/cancellation, fallback usage, playback start/end, and ElevenLabs
failure category (timeout / auth / rate_limit / connection / api). API keys,
authorization headers, and full transcripts are never logged.
