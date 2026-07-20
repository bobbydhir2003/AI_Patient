import type { VoiceConversationState } from "../../hooks/voiceStateMachine";
import styles from "./ConversationControl.module.css";

interface ConversationControlProps {
  patientName: string;
  supported: boolean;
  enabled: boolean; // backend session ready
  /** Saved transcript turns already exist (restored or live). The label makes
   * clear this button controls VOICE mode, not the conversation itself. */
  hasConversation?: boolean;
  state: VoiceConversationState;
  errorMessage: string | null;
  onStart: () => void;
  onStop: () => void;
  onInterrupt: () => void;
  onRetry: () => void;
}

function statusText(state: VoiceConversationState, patientName: string): string {
  switch (state) {
    case "IDLE":
      return "Ready";
    case "REQUESTING_PERMISSION":
      return "Requesting microphone access...";
    case "LISTENING":
      return "Listening to you...";
    case "PROCESSING":
      return "Processing your question...";
    case "SPEAKING":
      return `${patientName} is speaking...`;
    case "INTERRUPTING":
      return "Listening to your interruption...";
    case "COOLDOWN":
      return "One moment...";
    case "PAUSED":
      return "Conversation paused";
    case "ERROR":
      return "Voice conversation stopped";
    case "FINISHED":
      return "Interview finished";
  }
}

export function ConversationControl({
  patientName,
  supported,
  enabled,
  hasConversation = false,
  state,
  errorMessage,
  onStart,
  onStop,
  onInterrupt,
  onRetry,
}: ConversationControlProps) {
  if (!supported) {
    return (
      <div className={styles.wrapper}>
        <p className={styles.unsupported} role="note">
          Voice conversation is not supported in this browser. Please use Chrome, Edge, or
          Safari, or continue by typing.
        </p>
      </div>
    );
  }

  let mainLabel: string;
  let mainAria: string;
  let mainAction: (() => void) | null;
  switch (state) {
    case "IDLE":
      mainLabel = hasConversation ? "Resume Voice Conversation" : "Start Voice Conversation";
      mainAria = hasConversation
        ? `Resume voice conversation with ${patientName}`
        : `Start voice conversation with ${patientName}`;
      mainAction = onStart;
      break;
    case "PAUSED":
      mainLabel = "Resume Conversation";
      mainAria = `Resume voice conversation with ${patientName}`;
      mainAction = onStart;
      break;
    case "REQUESTING_PERMISSION":
      mainLabel = "Requesting microphone...";
      mainAria = "Requesting microphone access";
      mainAction = null;
      break;
    case "LISTENING":
      mainLabel = "Listening...";
      mainAria = "Listening. Activate to stop the voice conversation";
      mainAction = onStop;
      break;
    case "PROCESSING":
      mainLabel = "Processing...";
      mainAria = "Processing your question";
      mainAction = null;
      break;
    case "SPEAKING":
      mainLabel = `Interrupt ${patientName}`;
      mainAria = `Interrupt ${patientName} and start listening`;
      mainAction = onInterrupt;
      break;
    case "INTERRUPTING":
      mainLabel = "Listening to your interruption...";
      mainAria = "Listening to your interruption";
      mainAction = null;
      break;
    case "COOLDOWN":
      mainLabel = "One moment...";
      mainAria = "Preparing to listen";
      mainAction = null;
      break;
    case "ERROR":
      mainLabel = "Retry Voice Conversation";
      mainAria = "Retry the voice conversation";
      mainAction = onRetry;
      break;
    case "FINISHED":
      mainLabel = "Conversation finished";
      mainAria = "Voice conversation finished";
      mainAction = null;
      break;
  }

  const showStop =
    state === "LISTENING" ||
    state === "PROCESSING" ||
    state === "SPEAKING" ||
    state === "INTERRUPTING" ||
    state === "COOLDOWN" ||
    state === "ERROR";

  const stateClass = styles[`state_${state.toLowerCase()}`] ?? "";

  return (
    <div className={styles.wrapper}>
      <div className={styles.row}>
        <button
          type="button"
          className={`btn btn-primary ${styles.mainButton} ${stateClass}`}
          onClick={mainAction ?? undefined}
          disabled={mainAction === null || (!enabled && (state === "IDLE" || state === "PAUSED"))}
          aria-label={mainAria}
        >
          {state === "LISTENING" && <span className={styles.pulseDot} aria-hidden="true" />}
          {state === "SPEAKING" && (
            <span className={styles.speakerBars} aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          )}
          {state === "PROCESSING" && (
            <span className={styles.processingDots} aria-hidden="true">
              <span>.</span>
              <span>.</span>
              <span>.</span>
            </span>
          )}
          {mainLabel}
        </button>
        {showStop && (
          <button
            type="button"
            className={`btn btn-secondary ${styles.stopButton}`}
            onClick={onStop}
            aria-label="Stop voice conversation"
          >
            Stop
          </button>
        )}
      </div>
      <p className={styles.status} aria-live="polite">
        {statusText(state, patientName)}
        {state === "ERROR" && errorMessage ? ` — ${errorMessage}` : ""}
      </p>
    </div>
  );
}
