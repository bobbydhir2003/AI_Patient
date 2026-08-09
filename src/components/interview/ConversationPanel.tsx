import { useCallback, useEffect, useRef, type ReactNode } from "react";
import type { ConversationMessage } from "../../types/interview";
import { MessageBubble } from "./MessageBubble";
import styles from "./ConversationPanel.module.css";

interface ConversationPanelProps {
  messages: ConversationMessage[];
  isPatientResponding: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  inputDisabled?: boolean;
  sendDisabled?: boolean;
  patientName: string;
  /** Optional instructional card rendered ABOVE the scrollable transcript (it
   * stays fixed while the transcript scrolls). */
  welcome?: ReactNode;
  /** Voice conversation control rendered below the transcript (fixed). */
  voiceControl?: ReactNode;
  /** Composer mic button. Wires into the SAME voice hook as the big button
   * (start when idle, stop when active) — no separate voice system. Hidden when
   * omitted or unsupported. */
  mic?: {
    supported: boolean;
    active: boolean;
    disabled?: boolean;
    label: string;
    onToggle: () => void;
  };
}

/** Simple microphone glyph (decorative; the button carries the aria-label). */
function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
    </svg>
  );
}

/** Distance (px) from the bottom within which we consider the user "at bottom"
 * and therefore safe to auto-scroll on a new message. */
const NEAR_BOTTOM_PX = 120;

export function ConversationPanel({
  messages,
  isPatientResponding,
  draft,
  onDraftChange,
  onSend,
  inputDisabled = false,
  sendDisabled = false,
  patientName,
  welcome,
  voiceControl,
  mic,
}: ConversationPanelProps) {
  const transcriptRef = useRef<HTMLDivElement>(null);
  // Whether the user was near the bottom at the last scroll event. Starts true
  // so the first messages auto-scroll into view.
  const nearBottomRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = transcriptRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    nearBottomRef.current = distance <= NEAR_BOTTOM_PX;
  }, []);

  // Modern chat auto-scroll: only follow new messages when the user is already
  // near the bottom. If they scrolled up to read history, don't yank them down.
  useEffect(() => {
    const el = transcriptRef.current;
    if (!el || !nearBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, isPatientResponding]);

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      if (!sendDisabled) onSend();
    }
  }

  return (
    <div className={`card ${styles.panel}`}>
      {/* Fixed instructional card (does not scroll with the transcript). */}
      {welcome && <div className={styles.welcomeSlot}>{welcome}</div>}

      {/* ONLY this region scrolls. */}
      <div
        className={styles.messages}
        role="log"
        aria-live="polite"
        ref={transcriptRef}
        onScroll={onScroll}
      >
        {messages.length === 0 ? (
          <p className={styles.emptyState}>
            Start the interview by asking a question below — by voice or by typing.
          </p>
        ) : (
          messages.map((message) => (
            <MessageBubble key={message.id} message={message} patientName={patientName} />
          ))
        )}
        {isPatientResponding && (
          <p className={styles.typingIndicator}>Patient is responding...</p>
        )}
      </div>

      {/* Fixed controls (voice + composer) — always visible. */}
      {voiceControl && <div className={styles.voiceSlot}>{voiceControl}</div>}
      <div className={styles.inputBar}>
        <label className="visually-hidden" htmlFor="student-message-input">
          Type your question for the patient
        </label>
        <input
          id="student-message-input"
          className={`text-input ${styles.textInput}`}
          type="text"
          placeholder="Type your question..."
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={inputDisabled}
        />
        {mic?.supported && (
          <button
            type="button"
            className={`${styles.micButton} ${mic.active ? styles.micActive : ""}`}
            onClick={mic.onToggle}
            disabled={mic.disabled}
            aria-label={mic.label}
            aria-pressed={mic.active}
          >
            <MicIcon />
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary"
          onClick={onSend}
          disabled={sendDisabled}
        >
          Send
        </button>
      </div>
    </div>
  );
}
