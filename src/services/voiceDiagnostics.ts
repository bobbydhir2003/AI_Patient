/**
 * Production-safe patient-voice diagnostics.
 *
 * Two jobs, both metadata-only:
 *  1. Classified event logging so the NEXT incident can be diagnosed from
 *     ordinary browser logs — no need to reproduce with real students. Failures
 *     and fallbacks log at warn level (visible in production); routine
 *     lifecycle events log only in dev to avoid noise.
 *  2. Lightweight in-memory counters (ElevenLabs requested/succeeded/failed,
 *     playback failed, browser fallback used) readable via getVoiceCounters()
 *     for a quick health snapshot.
 *
 * NEVER logs: patient text, API keys, auth tokens, or any transcript content.
 * Only metadata (caseId, sentence index, correlationId, HTTP status, DOM error
 * name/message, fallback reason) is emitted.
 */

export type VoiceEvent =
  | "tts_requested"
  | "tts_succeeded"
  | "tts_fetch_failed"
  | "tts_empty_audio"
  | "tts_audio_play_failed"
  | "tts_media_source_failed"
  | "tts_source_buffer_failed"
  | "tts_progressive_start_timeout"
  | "tts_blob_fallback"
  | "tts_cancelled"
  | "tts_browser_fallback"
  // --- Atomic-path staged events (status probe / HTTP / decode / playback) ---
  // These give the atomic (non-streaming) voice path the same per-stage
  // visibility the streaming path already had, so a robotic-voice report can
  // be attributed to ONE of: the status probe, the /synthesize HTTP call,
  // audio decode, or audio.play() — instead of a single generic "TTS failed".
  | "voice_status_ok"
  | "voice_status_failed"
  | "voice_status_confirmed_unavailable"
  | "tts_request_started"
  | "tts_http_success"
  | "tts_http_failed"
  | "audio_blob_ready"
  | "audio_decode_failed"
  | "audio_play_started"
  | "audio_play_success"
  | "audio_play_failed"
  | "browser_fallback_started";

/** Stage-level failure category. See file header for how these map to the
 * report categories STATUS_PROBE_TRANSIENT / STATUS_CONFIRMED_UNAVAILABLE /
 * TTS_HTTP_ERROR / TTS_TIMEOUT / AUDIO_DECODE_ERROR / AUDIO_PLAY_NOT_ALLOWED /
 * AUDIO_PLAY_ABORTED / AUDIO_PLAY_UNKNOWN / BROWSER_FALLBACK. */
export type VoiceFailureCategory =
  | "STATUS_PROBE_TRANSIENT"
  | "STATUS_CONFIRMED_UNAVAILABLE"
  | "TTS_HTTP_ERROR"
  | "TTS_EMPTY_AUDIO"
  | "TTS_TIMEOUT"
  | "AUDIO_DECODE_ERROR"
  | "AUDIO_PLAY_NOT_ALLOWED"
  | "AUDIO_PLAY_ABORTED"
  | "AUDIO_PLAY_UNKNOWN"
  | "BROWSER_FALLBACK";

/** Metadata bag. Callers pass only non-sensitive fields (see file header). */
export interface VoiceEventMeta {
  caseId?: string;
  /** Sentence index within a streamed turn (undefined for the atomic path). */
  index?: number;
  correlationId?: string;
  /** HTTP status for fetch failures. */
  status?: number;
  /** DOMException / Error name + message for playback failures. */
  errorName?: string;
  errorMessage?: string;
  /** Why a browser-TTS fallback was chosen (e.g. "fetch_failed", "play_failed"). */
  reason?: string;
  /** Playback path in use, when relevant ("progressive" | "blob"). */
  path?: string;
  /** Stage-level failure classification (see VoiceFailureCategory). */
  category?: VoiceFailureCategory;
}

export interface VoiceCounters {
  requested: number;
  succeeded: number;
  fetchFailed: number;
  emptyAudio: number;
  playbackFailed: number;
  blobFallback: number;
  browserFallback: number;
  cancelled: number;
}

const counters: VoiceCounters = {
  requested: 0,
  succeeded: 0,
  fetchFailed: 0,
  emptyAudio: 0,
  playbackFailed: 0,
  blobFallback: 0,
  browserFallback: 0,
  cancelled: 0,
};

const COUNTER_FOR: Partial<Record<VoiceEvent, keyof VoiceCounters>> = {
  tts_requested: "requested",
  tts_succeeded: "succeeded",
  tts_fetch_failed: "fetchFailed",
  tts_empty_audio: "emptyAudio",
  tts_audio_play_failed: "playbackFailed",
  tts_media_source_failed: "playbackFailed",
  tts_source_buffer_failed: "playbackFailed",
  tts_progressive_start_timeout: "playbackFailed",
  tts_blob_fallback: "blobFallback",
  tts_browser_fallback: "browserFallback",
  tts_cancelled: "cancelled",
  // Atomic-path staged events share the same counters as their streaming-path
  // equivalents (they measure the same thing, just on the other code path).
  tts_request_started: "requested",
  tts_http_success: "succeeded",
  tts_http_failed: "fetchFailed",
  audio_decode_failed: "playbackFailed",
  audio_play_failed: "playbackFailed",
  browser_fallback_started: "browserFallback",
  voice_status_failed: "fetchFailed",
};

/** Events surfaced at warn level in production (diagnostic signal for incidents).
 * Cancellation is intentionally NOT here — it is normal, not a failure.
 * voice_status_failed and voice_status_confirmed_unavailable are ALSO not
 * plain failures on their own (see patientVoiceService.getVoiceStatus): a
 * transient probe failure no longer forces browser TTS, so it is logged at
 * warn for visibility but is not, by itself, "voice broke for this student". */
const WARN_EVENTS = new Set<VoiceEvent>([
  "tts_fetch_failed",
  "tts_empty_audio",
  "tts_audio_play_failed",
  "tts_media_source_failed",
  "tts_source_buffer_failed",
  "tts_progressive_start_timeout",
  "tts_blob_fallback",
  "tts_browser_fallback",
  "voice_status_failed",
  "tts_http_failed",
  "audio_decode_failed",
  "audio_play_failed",
  "browser_fallback_started",
]);

function isDev(): boolean {
  try {
    return Boolean((import.meta as unknown as { env?: { DEV?: boolean } }).env?.DEV);
  } catch {
    return false;
  }
}

/** Extract just the safe name/message from an unknown thrown value. */
export function describeError(err: unknown): { errorName?: string; errorMessage?: string } {
  if (err && typeof err === "object") {
    const e = err as { name?: unknown; message?: unknown };
    return {
      errorName: typeof e.name === "string" ? e.name : undefined,
      errorMessage: typeof e.message === "string" ? e.message : undefined,
    };
  }
  return { errorMessage: typeof err === "string" ? err : undefined };
}

/** Record a classified voice event (metadata only). Never throws. */
export function logVoiceEvent(event: VoiceEvent, meta: VoiceEventMeta = {}): void {
  const key = COUNTER_FOR[event];
  if (key) counters[key] += 1;
  try {
    const line = `[voice] ${event}`;
    if (WARN_EVENTS.has(event)) {
      // Visible in production so incidents are diagnosable from browser logs.
      console.warn(line, meta);
    } else if (isDev()) {
      console.debug(line, meta);
    }
  } catch {
    /* logging must never affect playback */
  }
}

/** Snapshot of the voice counters (for a quick health check / diagnostics). */
export function getVoiceCounters(): VoiceCounters {
  return { ...counters };
}

/** Test/diagnostics hook: reset the counters. */
export function _resetVoiceCountersForTests(): void {
  (Object.keys(counters) as (keyof VoiceCounters)[]).forEach((k) => {
    counters[k] = 0;
  });
}
