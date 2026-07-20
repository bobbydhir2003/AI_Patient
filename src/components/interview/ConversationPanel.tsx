import { useEffect, useRef, type ReactNode } from "react";
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
  patientImage: string;
  patientName: string;
  /** Voice conversation control rendered above the input bar. */
  voiceControl?: ReactNode;
}

export function ConversationPanel({
  messages,
  isPatientResponding,
  draft,
  onDraftChange,
  onSend,
  inputDisabled = false,
  sendDisabled = false,
  patientImage,
  patientName,
  voiceControl,
}: ConversationPanelProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isPatientResponding]);

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      if (!sendDisabled) onSend();
    }
  }

  return (
    <div className={`card ${styles.panel}`}>
      <div className={styles.messages} role="log" aria-live="polite">
        {messages.length === 0 ? (
          <p className={styles.emptyState}>
            Start the interview by asking the patient a question below.
          </p>
        ) : (
          messages.map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              patientImage={patientImage}
              patientName={patientName}
            />
          ))
        )}
        {isPatientResponding && (
          <p className={styles.typingIndicator}>Patient is responding...</p>
        )}
        <div ref={messagesEndRef} />
      </div>
      {voiceControl}
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
