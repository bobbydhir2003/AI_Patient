import styles from "./InterviewWelcomeCard.module.css";

interface InterviewWelcomeCardProps {
  /** Patient/case display name — fully data-driven, never hardcoded. */
  patientName: string;
}

/**
 * Subtle instructional card shown at the top of the transcript. It contains NO
 * clinical facts about any specific case — the real patient information always
 * comes from the AI conversation/backend. Only the patient's display name is
 * interpolated, so this works for every case automatically.
 */
export function InterviewWelcomeCard({ patientName }: InterviewWelcomeCardProps) {
  return (
    <div className={styles.card} role="note">
      <span className={styles.icon} aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
          <path d="M12 2l1.6 4.4L18 8l-4.4 1.6L12 14l-1.6-4.4L6 8l4.4-1.6L12 2Z" />
          <path d="M18 13l.8 2.2L21 16l-2.2.8L18 19l-.8-2.2L15 16l2.2-.8L18 13Z" />
        </svg>
      </span>
      <div className={styles.body}>
        <p className={styles.title}>Welcome! You are now interviewing {patientName}.</p>
        <p className={styles.text}>
          Ask questions to gather relevant information about the patient's condition, symptoms,
          history, and how they affect daily activities.
        </p>
      </div>
    </div>
  );
}
