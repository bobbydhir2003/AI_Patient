/**
 * Pure, side-effect-free state machine for the voice conversation.
 * All browser/audio side effects live in useVoiceConversation; this module is
 * deliberately dependency-free so it can be unit-tested in Node.
 *
 * Critical invariants encoded here:
 * - LISTENING and SPEAKING are distinct states and can never coexist.
 * - A final transcript is only accepted while LISTENING (prevents duplicates).
 * - An interruption is only possible while SPEAKING.
 * - A short noise spike (VAD_SPIKE) never changes state; only sustained voice
 *   (VAD_SUSTAINED_VOICE) interrupts the patient.
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

export type VoiceEvent =
  | { type: "START" }
  | { type: "PERMISSION_GRANTED" }
  | { type: "PERMISSION_DENIED"; message: string }
  | { type: "FINAL_TRANSCRIPT"; text: string }
  | { type: "SUBMIT_ACCEPTED" }
  | { type: "RESPONSE_RECEIVED"; speak: boolean }
  | { type: "RESPONSE_FAILED"; message: string }
  | { type: "TTS_ENDED" }
  | { type: "INTERRUPT" } // manual button, typed-send barge-in, or sustained VAD
  | { type: "VAD_SPIKE" } // short noise; must be ignored
  | { type: "VAD_SUSTAINED_VOICE" }
  | { type: "INTERRUPT_READY" } // TTS cancelled + settle delay elapsed
  | { type: "COOLDOWN_ELAPSED" }
  | { type: "RECOGNITION_ERROR"; message: string; fatal: boolean }
  | { type: "STOP" }
  | { type: "RESUME" }
  | { type: "RETRY" }
  | { type: "FINISH" }
  | { type: "RESET" };

export interface VoiceMachine {
  state: VoiceConversationState;
  errorMessage: string | null;
  /** transcript accepted for submission on the last FINAL_TRANSCRIPT event */
  acceptedTranscript: string | null;
}

export const initialMachine: VoiceMachine = {
  state: "IDLE",
  errorMessage: null,
  acceptedTranscript: null,
};

/** Minimum quality gate for a recognized transcript: at least one word with
 * two or more letters. Rejects empty results and single meaningless sounds. */
export function isUsableTranscript(text: string): boolean {
  const trimmed = text.trim();
  if (trimmed.length < 2) return false;
  return /[a-zA-Z]{2,}/.test(trimmed);
}

export function reduce(machine: VoiceMachine, event: VoiceEvent): VoiceMachine {
  const { state } = machine;
  const to = (
    next: VoiceConversationState,
    extra: Partial<VoiceMachine> = {},
  ): VoiceMachine => ({
    state: next,
    errorMessage: null,
    acceptedTranscript: null,
    ...extra,
  });

  switch (event.type) {
    case "RESET":
      return { ...initialMachine };
    case "FINISH":
      return to("FINISHED");
    case "STOP":
      // Stopping is allowed from any active state; keeps voice mode resumable.
      if (state === "IDLE" || state === "FINISHED") return machine;
      return to("PAUSED");
    case "START":
      return state === "IDLE" ? to("REQUESTING_PERMISSION") : machine;
    case "RESUME":
      return state === "PAUSED" ? to("REQUESTING_PERMISSION") : machine;
    case "PERMISSION_GRANTED":
      return state === "REQUESTING_PERMISSION" ? to("LISTENING") : machine;
    case "PERMISSION_DENIED":
      return state === "REQUESTING_PERMISSION"
        ? to("ERROR", { errorMessage: event.message })
        : machine;
    case "FINAL_TRANSCRIPT": {
      if (state !== "LISTENING") return machine; // duplicate/late results ignored
      if (!isUsableTranscript(event.text)) return machine; // noise: keep listening
      return to("PROCESSING", { acceptedTranscript: event.text.trim() });
    }
    case "SUBMIT_ACCEPTED":
      return machine; // informational; already in PROCESSING
    case "RESPONSE_RECEIVED":
      if (state !== "PROCESSING") return machine;
      return event.speak ? to("SPEAKING") : to("COOLDOWN");
    case "RESPONSE_FAILED":
      return state === "PROCESSING" ? to("ERROR", { errorMessage: event.message }) : machine;
    case "TTS_ENDED":
      return state === "SPEAKING" ? to("COOLDOWN") : machine;
    case "INTERRUPT":
    case "VAD_SUSTAINED_VOICE":
      return state === "SPEAKING" ? to("INTERRUPTING") : machine;
    case "VAD_SPIKE":
      return machine; // never a state change
    case "INTERRUPT_READY":
      return state === "INTERRUPTING" ? to("LISTENING") : machine;
    case "COOLDOWN_ELAPSED":
      return state === "COOLDOWN" ? to("LISTENING") : machine;
    case "RECOGNITION_ERROR":
      if (state !== "LISTENING" && state !== "INTERRUPTING") return machine;
      return event.fatal ? to("ERROR", { errorMessage: event.message }) : machine;
    case "RETRY":
      return state === "ERROR" ? to("REQUESTING_PERMISSION") : machine;
    default:
      return machine;
  }
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
