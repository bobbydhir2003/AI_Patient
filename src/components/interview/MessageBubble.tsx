import type { ConversationMessage } from "../../types/interview";
import { AppImage } from "../common/AppImage";
import styles from "./MessageBubble.module.css";

interface MessageBubbleProps {
  message: ConversationMessage;
  /** Kept for accessible labels only — the transcript never shows the real
   * patient photograph (that belongs to the left profile panel). */
  patientName?: string;
  patientImage?: string;
}

/** Generic clinician/student icon. Decorative (aria-hidden); the row carries
 * the accessible sender via the bubble's own aria-label. */
function IconUser() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="3.4" />
      <path d="M5 20c0-3.4 3.1-5.6 7-5.6s7 2.2 7 5.6" />
    </svg>
  );
}

/** Generic patient/person icon (not a photo). */
function IconPatient() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="7.5" r="3.6" />
      <path d="M4.5 20a7.5 7.5 0 0 1 15 0" />
    </svg>
  );
}

export function MessageBubble({ message, patientName, patientImage }: MessageBubbleProps) {
  const isPatient = message.sender === "patient";
  const rowClass = `${styles.row} ${isPatient ? styles.patient : styles.student}`;
  // Prefer the case's real speaker label ("Camden's Mother", "Camden", etc.) so
  // caregiver-led cases stay correct; fall back to a neutral role word.
  const who = isPatient ? message.speakerLabel || patientName || "Patient" : "You";
  const usePatientImage =
    isPatient &&
    !!patientImage &&
    (!message.speakerLabel || message.speakerLabel === patientName);

  const icon = (
    usePatientImage ? (
      <AppImage
        src={patientImage}
        alt={`${patientName || "Patient"} avatar`}
        className={`${styles.avatar} ${styles.avatarImage}`}
      />
    ) : (
      <span className={`${styles.avatar} ${isPatient ? styles.avatarPatient : styles.avatarStudent}`}>
        {isPatient ? <IconPatient /> : <IconUser />}
      </span>
    )
  );

  return (
    <div className={rowClass}>
      {isPatient && icon}
      <div className={styles.bubble} aria-label={`${who} said`}>
        {isPatient && message.speakerLabel && (
          <span className={styles.speaker}>{message.speakerLabel}</span>
        )}
        <span className={styles.text}>{message.text}</span>
        {message.timestamp && <span className={styles.timestamp}>{message.timestamp}</span>}
      </div>
      {!isPatient && icon}
    </div>
  );
}
