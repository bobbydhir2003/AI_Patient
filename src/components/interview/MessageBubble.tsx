import type { ConversationMessage } from "../../types/interview";
import { AppImage } from "../common/AppImage";
import styles from "./MessageBubble.module.css";

interface MessageBubbleProps {
  message: ConversationMessage;
  patientImage?: string;
  patientName?: string;
}

export function MessageBubble({ message, patientImage, patientName }: MessageBubbleProps) {
  const isPatient = message.sender === "patient";
  const rowClass = `${styles.row} ${isPatient ? styles.patient : styles.student}`;

  return (
    <div className={rowClass}>
      {isPatient && patientImage && (
        <AppImage
          src={patientImage}
          alt={`${patientName ?? "Patient"} avatar`}
          className={styles.avatar}
        />
      )}
      <div className={styles.bubble}>
        {isPatient && message.speakerLabel && (
          <span className={styles.speaker}>{message.speakerLabel}</span>
        )}
        <span className={styles.text}>{message.text}</span>
        <span className={styles.timestamp}>{message.timestamp}</span>
      </div>
    </div>
  );
}
