/**
 * Production-safe patient-voice diagnostics.
 *
 * Three jobs, all metadata-only:
 *  1. Classified event logging so the NEXT incident can be diagnosed from
 *     ordinary browser logs — no need to reproduce with real students. Failures
 *     and fallbacks log at warn level (visible in production); routine
 *     lifecycle events log only in dev to avoid noise.
 *  2. Lightweight in-memory counters (ElevenLabs requested/succeeded/failed,
 *     playback failed, browser fallback used) readable via getVoiceCounters()
 *     for a quick health snapshot.
 *  3. A small, fire-and-forget POST to the backend (POST /api/voice/telemetry)
 *     for the SAME events, so a mobile robotic-fallback incident can finally
 *     be correlated server-side with the matching tts_request_start/complete
 *     log line via correlationId - closing the "console-only" observability
 *     gap. Never awaited by callers, never retried, never throws, never
 *     blocks or delays playback in any way.
 *
 * NEVER logs (locally or remotely): patient text, API keys, auth tokens, or
 * any transcript content. Only metadata (caseId, sentence index,
 * correlationId, HTTP status, DOM error name/message, fallback reason) is
 * emitted.
 */
import { API_BASE_URL, withAuthHeaders } from "./api";
import { deviceCategory } from "./mobileAudio";

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
  | "browser_fallback_started"
  // --- 409 (no TTS capacity slot) bounded single retry. ElevenLabs was never
  // contacted for a 409, so this retry costs no provider capacity - see
  // TTS_CAPACITY_RETRY_DELAY_MS in patientVoiceService.ts. Exactly one retry,
  // never more; every other failure (502/5xx/timeout/empty) is NOT retried
  // here since the backend already exhausted its own provider retry loop.
  | "tts_capacity_retry_scheduled"
  | "tts_capacity_retry_started"
  | "tts_capacity_retry_succeeded"
  | "tts_capacity_retry_failed"
  // --- Mobile playback strategy + user-gesture recovery (see the mobile
  // voice reliability audit and patientVoiceService.ts). "mobile_buffered_first"
  // fires once when a device (currently: iOS) is routed to buffered playback
  // instead of progressive MediaSource. The recovery events fire only when
  // ElevenLabs generated valid audio but playback itself failed - never for a
  // confirmed generation failure (409-exhausted/502/5xx/timeout/empty).
  | "mobile_buffered_first"
  | "audio_user_gesture_recovery_offered"
  | "audio_user_gesture_recovery_clicked"
  | "audio_user_gesture_recovery_success"
  | "audio_user_gesture_recovery_failed"
  // --- Phase 1 LiveKit POC only (src/services/livekit/, LiveKitTestPage) -
  // NOT emitted by the production interview/voice path. Mirrors the same
  // metadata-only, no-patient-text discipline as every event above.
  | "livekit_room_connecting"
  | "livekit_room_connected"
  | "livekit_room_disconnected"
  | "livekit_room_reconnecting"
  | "livekit_room_reconnected"
  | "livekit_mic_published"
  | "livekit_patient_track_subscribed"
  | "livekit_agent_started"
  | "livekit_patient_audio_started"
  | "livekit_patient_audio_completed"
  | "livekit_patient_audio_failed"
  // --- readiness/timeout diagnosability (see the 4-device production-test
  // inspection). Answers, from logs alone, whether a patient_turn_status
  // data message arrived at all, whether it matched the pending turn, and
  // exactly when the thinking-timeout watchdog was armed/cancelled/fired -
  // without needing to reproduce the incident live.
  | "livekit_turn_status_received"
  | "livekit_turn_status_matched"
  | "livekit_turn_status_ignored"
  | "livekit_thinking_timeout_started"
  | "livekit_thinking_timeout_cancelled"
  | "livekit_thinking_timeout_fired"
  | "livekit_audio_element_attached"
  | "livekit_audio_playing"
  | "livekit_audio_play_failed"
  | "livekit_engine_error"
  // --- Production reliability protocol (agent-ready handshake + turn
  // delivery ACK + bounded automatic retry - see livekitPocEngine.ts's
  // module docstring for the confirmed production incident this answers).
  // Supersedes the old "livekit_first_turn_sent" event with a pair that
  // distinguishes "about to call publishData" from "publishData resolved",
  // closing the exact gap a prior forensic inspection identified.
  | "livekit_agent_ready_received"
  | "livekit_turn_publish_started"
  | "livekit_turn_publish_resolved"
  | "livekit_turn_ack_received"
  | "livekit_turn_ack_timeout"
  | "livekit_turn_auto_retry"
  | "livekit_turn_delivery_failed";

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
  /** Elapsed milliseconds for this event, when relevant (e.g. time from a
   * LiveKit turn being sent to the patient's first audio). Bounded/validated
   * server-side (VoiceTelemetryEvent.duration_ms) - never patient content. */
  durationMs?: number;
  /** LiveKitPocEngine's PocState at the moment of this event (e.g. "thinking",
   * "error"). Engine lifecycle only - never patient content. */
  engineState?: string;
  /** The patient_turn_status data message's status field ("speaking_started" |
   * "speaking_ended" | "failed"), or a diagnostic outcome for that message
   * ("received" | "matched" | "parse_error" | "client_turn_id_mismatch" |
   * "unsupported_status"). Never patient text. */
  turnStatus?: string;
  /** Retry attempt number for the bounded automatic turn-delivery retry
   * (see livekitPocEngine.ts's armDeliveryWatchdog) - 0 for the first
   * (non-retry) attempt. Never patient content. */
  attempt?: number;
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
  audio_user_gesture_recovery_failed: "playbackFailed",
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
  "tts_capacity_retry_failed",
  "audio_user_gesture_recovery_failed",
  "livekit_patient_audio_failed",
  "livekit_thinking_timeout_fired",
  "livekit_audio_play_failed",
  "livekit_engine_error",
  "livekit_turn_ack_timeout",
  "livekit_turn_auto_retry",
  "livekit_turn_delivery_failed",
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

/** Events the backend schema (VoiceTelemetryEvent) actually accepts. Kept as
 * an explicit allowlist (mirroring the backend's Literal[...] validation) so
 * a locally-added frontend-only event never produces a noisy 422 in the
 * network tab - it just silently isn't shipped until the backend list is
 * updated to match. */
const TELEMETRY_EVENTS = new Set<VoiceEvent>([
  "voice_status_ok", "voice_status_failed", "voice_status_confirmed_unavailable",
  "tts_requested", "tts_request_started", "tts_succeeded", "tts_fetch_failed",
  "tts_http_success", "tts_http_failed", "tts_empty_audio",
  "audio_blob_ready", "audio_decode_failed", "audio_play_started",
  "audio_play_success", "audio_play_failed",
  "mobile_buffered_first",
  "audio_user_gesture_recovery_offered", "audio_user_gesture_recovery_clicked",
  "audio_user_gesture_recovery_success", "audio_user_gesture_recovery_failed",
  "browser_fallback_started",
  "tts_capacity_retry_scheduled", "tts_capacity_retry_started",
  "tts_capacity_retry_succeeded", "tts_capacity_retry_failed",
  "tts_cancelled",
  "livekit_room_connecting", "livekit_room_connected", "livekit_room_disconnected",
  "livekit_room_reconnecting", "livekit_room_reconnected", "livekit_mic_published",
  "livekit_patient_track_subscribed", "livekit_agent_started",
  "livekit_patient_audio_started", "livekit_patient_audio_completed",
  "livekit_patient_audio_failed",
  "livekit_turn_status_received",
  "livekit_turn_status_matched", "livekit_turn_status_ignored",
  "livekit_thinking_timeout_started", "livekit_thinking_timeout_cancelled",
  "livekit_thinking_timeout_fired", "livekit_audio_element_attached",
  "livekit_audio_playing", "livekit_audio_play_failed", "livekit_engine_error",
  "livekit_agent_ready_received", "livekit_turn_publish_started",
  "livekit_turn_publish_resolved", "livekit_turn_ack_received",
  "livekit_turn_ack_timeout", "livekit_turn_auto_retry", "livekit_turn_delivery_failed",
]);

/**
 * Fire-and-forget: ship this event to POST /api/voice/telemetry so it can be
 * correlated server-side. Never awaited, never retried, swallows every
 * failure silently (a telemetry outage must never be visible to a student or
 * affect playback in any way). `keepalive` lets the request survive a page
 * navigation immediately after a turn ends.
 */
function sendTelemetry(event: VoiceEvent, meta: VoiceEventMeta): void {
  if (!TELEMETRY_EVENTS.has(event)) return;
  try {
    const body = JSON.stringify({
      event,
      correlationId: meta.correlationId ?? "",
      caseId: meta.caseId ?? "",
      status: meta.status ?? null,
      category: meta.category ?? "",
      deviceCategory: deviceCategory(),
      playbackMethod: meta.path ?? "",
      durationMs: meta.durationMs ?? null,
      engineState: meta.engineState ?? "",
      turnStatus: meta.turnStatus ?? "",
      attempt: meta.attempt ?? null,
      reason: meta.reason ?? "",
    });
    void fetch(`${API_BASE_URL}/api/voice/telemetry`, {
      method: "POST",
      headers: withAuthHeaders({ "Content-Type": "application/json" }),
      body,
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    /* telemetry must never affect playback */
  }
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
  sendTelemetry(event, meta);
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
