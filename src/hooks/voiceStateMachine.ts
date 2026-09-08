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
    state === "COOLDOWN"
  );
}
