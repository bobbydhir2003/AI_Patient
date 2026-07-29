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

/** Controlled speech-performance labels from the backend (delivery only -
 * they never change the transcript text or assessment input). */
export interface PatientSpeechStyle {
  emotion?: string;
  pace?: string;
  energy?: string;
  hesitation?: string;
  pauseBeforeMs?: number;
}

/** Handle for an in-progress STREAMING exchange (structural type so the
 * interview types stay dependency-free; implemented by patientStreamService). */
export interface StreamingExchangeRef {
  clientTurnId: string;
  completion: Promise<{
    turnId: string;
    patientText: string;
    status: string;
    speech: PatientSpeechStyle | null;
  } | null>;
  playbackDone: Promise<void>;
  setOnPlaybackStart(cb: (() => void) | null): void;
  cancel(): void;
  isCancelled(): boolean;
}

/** One completed exchange: the approved patient text plus everything the TTS
 * layer needs to voice it (turn reference + delivery labels). When `streaming`
 * is present, the patient may ALREADY be speaking: audio and transcript growth
 * are owned by the streaming handle, and patientText holds the text so far. */
export interface PatientExchange {
  patientText: string;
  turnId: string;
  speech: PatientSpeechStyle | null;
  streaming?: StreamingExchangeRef;
}

/** Voice conversation state machine (single source of truth for voice mode).
 * LISTENING and SPEAKING can never run at the same time. */
export type { VoiceConversationState } from "../hooks/voiceStateMachine";
