/**
 * Pure, dependency-free playback bookkeeping for the patient voice service.
 * Encodes the race-condition rules so they can be unit-tested in Node:
 *
 * - Every speak request gets a monotonically increasing generation id.
 * - Only the LATEST generation may play audio or fall back; anything older is
 *   stale (a late network response, an out-of-order completion, a response
 *   arriving after cancellation) and must be silently dropped.
 * - Cancellation invalidates ALL outstanding generations at once.
 *
 * All browser side effects (fetch, Audio elements, object URLs) live in
 * patientVoiceService; this module is the single source of truth for which
 * playback is allowed to proceed.
 */

export type TtsProvider = "elevenlabs" | "browser";

export interface PlaybackGuard {
  /** Register a new speak request; returns its generation id. */
  begin(): number;
  /** True only for the latest, non-cancelled generation. */
  isCurrent(generation: number): boolean;
  /** Invalidate every outstanding generation (cancellation / teardown). */
  invalidateAll(): void;
  /** The current generation (diagnostics/logging only). */
  current(): number;
}

export function createPlaybackGuard(): PlaybackGuard {
  let generation = 0;
  return {
    begin() {
      generation += 1;
      return generation;
    },
    isCurrent(g: number) {
      return g === generation;
    },
    invalidateAll() {
      generation += 1;
    },
    current() {
      return generation;
    },
  };
}

/**
 * Provider decision: ElevenLabs only when the backend reports it available for
 * this case; otherwise (including unknown/unreachable status) browser TTS.
 */
export function chooseProvider(elevenLabsAvailable: boolean | undefined): TtsProvider {
  return elevenLabsAvailable === true ? "elevenlabs" : "browser";
}

/** Clamp the client-side pre-speech pause to the same safe range the backend
 * enforces, so a malformed header can never stall playback. */
export function clampPauseMs(value: unknown, fallback = 150): number {
  if (value === null || value === undefined || value === "") return fallback;
  const n = typeof value === "string" ? Number.parseInt(value, 10) : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(1500, Math.trunc(n)));
}

/**
 * Pause overlap: the natural `pauseBeforeMs` starts ticking when the TTS
 * REQUEST starts, not after the audio arrives. Before playback, wait only for
 * whatever portion of the pause the network hasn't already consumed.
 *
 *   pause 500 ms, audio ready after 800 ms → wait 0 ms
 *   pause 500 ms, audio ready after 200 ms → wait 300 ms
 */
export function remainingPauseMs(pauseBeforeMs: unknown, elapsedSinceRequestMs: number): number {
  const pause = clampPauseMs(pauseBeforeMs);
  const elapsed = Number.isFinite(elapsedSinceRequestMs) ? Math.max(0, elapsedSinceRequestMs) : 0;
  return Math.max(0, pause - elapsed);
}

// ---------------------------------------------------------------------------
// Progressive playback phases (MediaSource path).
//
// Pure reducer so the cancellation rules can be unit-tested in Node:
// - chunks may only be appended while the session is active (streaming/playing)
// - playback may only start once at least one chunk has been appended
// - CANCEL and FAIL are terminal from ANY phase (before the first chunk,
//   during buffering, during playback, near the end) and absorb every later
//   event: a stale chunk or completion event after cancellation is ignored.
// ---------------------------------------------------------------------------

export type ProgressivePhase =
  | "requested" // TTS request sent; no audio data yet
  | "streaming" // >=1 chunk received; buffering (playback not started)
  | "playing" // audio element is playing while chunks may still arrive
  | "ended" // playback finished normally
  | "cancelled" // interrupted / superseded
  | "failed"; // network, append, or decode error

export interface ProgressiveState {
  phase: ProgressivePhase;
  chunksReceived: number;
  streamComplete: boolean;
}

export const initialProgressiveState: ProgressiveState = {
  phase: "requested",
  chunksReceived: 0,
  streamComplete: false,
};

export type ProgressiveEvent =
  | { type: "CHUNK" }
  | { type: "PLAYBACK_STARTED" }
  | { type: "STREAM_ENDED" }
  | { type: "PLAYBACK_ENDED" }
  | { type: "CANCEL" }
  | { type: "FAIL" };

export function isTerminal(state: ProgressiveState): boolean {
  return state.phase === "ended" || state.phase === "cancelled" || state.phase === "failed";
}

/** Chunks may be appended only while the session is active. */
export function canAppendChunk(state: ProgressiveState): boolean {
  return state.phase === "requested" || state.phase === "streaming" || state.phase === "playing";
}

/** Playback may start only once buffering has begun (>=1 appended chunk). */
export function canStartPlayback(state: ProgressiveState): boolean {
  return state.phase === "streaming" && state.chunksReceived > 0;
}

export function reduceProgressive(
  state: ProgressiveState,
  event: ProgressiveEvent,
): ProgressiveState {
  // Terminal states absorb everything: stale chunks, late STREAM_ENDED or
  // PLAYBACK_ENDED events, duplicate cancels - none may change the outcome.
  if (isTerminal(state)) return state;

  switch (event.type) {
    case "CHUNK":
      if (!canAppendChunk(state)) return state;
      return {
        ...state,
        phase: state.phase === "requested" ? "streaming" : state.phase,
        chunksReceived: state.chunksReceived + 1,
      };
    case "PLAYBACK_STARTED":
      return canStartPlayback(state) ? { ...state, phase: "playing" } : state;
    case "STREAM_ENDED":
      return { ...state, streamComplete: true };
    case "PLAYBACK_ENDED":
      // Only meaningful once playing; an 'ended' from a detached element is stale.
      return state.phase === "playing" ? { ...state, phase: "ended" } : state;
    case "CANCEL":
      return { ...state, phase: "cancelled" };
    case "FAIL":
      return { ...state, phase: "failed" };
    default:
      return state;
  }
}
