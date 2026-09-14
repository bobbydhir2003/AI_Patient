/**
 * Shared voice-conversation state shape and small pure helpers, used by
 * useLiveKitInterviewVoice (which drives the actual state transitions) and by
 * the components that render against VoiceConversationState.
 */

export type VoiceConversationState =
  | "IDLE"
  | "REQUESTING_PERMISSION"
  | "LISTENING"
  | "PROCESSING"
  | "SPEAKING"
  | "INTERRUPTING"
  | "COOLDOWN"
  // Phase 1 lifecycle: a real, awaited teardown state. Stop enters STOPPING
  // immediately and the UI never shows IDLE again until the underlying
  // LiveKit room/mic/timers have fully finished tearing down - which is what
  // makes Start -> Stop -> Start safe (no overlap with a still-dying room).
  | "STOPPING"
  | "PAUSED"
  | "ERROR"
  | "FINISHED";

/** Minimum quality gate for a recognized transcript: at least one word with
 * two or more letters. Rejects empty results and single meaningless sounds. */
export function isUsableTranscript(text: string): boolean {
  const trimmed = text.trim();
  if (trimmed.length < 2) return false;
  return /[a-zA-Z]{2,}/.test(trimmed);
}

/** True while the conversation loop is running and owns the microphone. */
export function isConversationActive(state: VoiceConversationState): boolean {
  return (
    state === "REQUESTING_PERMISSION" ||
    state === "LISTENING" ||
    state === "PROCESSING" ||
    state === "SPEAKING" ||
    state === "INTERRUPTING" ||
    state === "COOLDOWN" ||
    // STOPPING still "owns" the mic/room until teardown resolves, so the UI
    // must keep treating it as active (show Stop, never offer a fresh Start).
    state === "STOPPING"
  );
}
