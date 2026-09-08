export type MessageSender = "student" | "patient";

export type MessageSaveStatus = "pending" | "saved" | "failed";
export type MessageSource = "typed" | "speech" | "openai";

export interface ConversationMessage {
  id: string;
  sender: MessageSender;
  text: string;
  timestamp: string;
  /** Frontend-generated idempotency id (mirrors the backend client_turn_id). */
  clientTurnId?: string;
  source?: MessageSource;
  /** Multi-participant speaker label (e.g. "Camden's Mother", "Camden").
   * Absent/blank for single-speaker cases. */
  speakerId?: string;
  speakerLabel?: string;
  /** Messages are rendered only after backend confirmation in the atomic
   * exchange flow, so rendered messages are "saved"; the field exists so any
   * future optimistic path must track persistence explicitly. */
  saveStatus?: MessageSaveStatus;
}

/** Backend connectivity for the interview screen. */
export type ConnectionState = "connecting" | "connected" | "offline" | "error";


/** Voice conversation state machine (single source of truth for voice mode).
 * LISTENING and SPEAKING can never run at the same time. */
export type { VoiceConversationState } from "../hooks/voiceStateMachine";
