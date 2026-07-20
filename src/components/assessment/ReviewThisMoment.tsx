import type { AssessmentEvidence } from "../../types/assessment";
import styles from "./assessment.module.css";

/** Structured, professional breakdown of an important mistake or missed
 * opportunity: what happened, why it mattered, and a stronger alternative. */
export function ReviewThisMoment({
  evidence,
  rubricDomain,
  onViewTranscript,
}: {
  evidence: AssessmentEvidence;
  rubricDomain: string;
  onViewTranscript: (turnId: string, evidenceId: string) => void;
}) {
  return (
    <div className={styles.momentBlock}>
      <span className={styles.momentHeading}>Review This Moment</span>
      {evidence.studentExcerpt && (
        <div className={styles.momentRow}>
          <span className={styles.momentLabel}>What you asked</span>
          <span className={styles.momentText}>&ldquo;{evidence.studentExcerpt}&rdquo;</span>
        </div>
      )}
      {evidence.patientExcerpt && (
        <div className={styles.momentRow}>
          <span className={styles.momentLabel}>What the patient said</span>
          <span className={styles.momentText}>&ldquo;{evidence.patientExcerpt}&rdquo;</span>
        </div>
      )}
      <div className={styles.momentRow}>
        <span className={styles.momentLabel}>Why the AI flagged it</span>
        <span className={styles.momentText}>{evidence.explanation}</span>
      </div>
      {evidence.whyItMatters && (
        <div className={styles.momentRow}>
          <span className={styles.momentLabel}>Why it matters</span>
          <span className={styles.momentText}>{evidence.whyItMatters}</span>
        </div>
      )}
      {evidence.suggestedAlternative && (
        <div className={styles.momentRow}>
          <span className={styles.momentLabel}>A stronger approach</span>
          <span className={`${styles.momentText} ${styles.momentAlt}`}>
            &ldquo;{evidence.suggestedAlternative}&rdquo;
          </span>
        </div>
      )}
      <div className={styles.momentRow}>
        <span className={styles.momentLabel}>
          Related rubric: {rubricDomain} · Transcript: {evidence.turnLabel}
        </span>
      </div>
      <button
        type="button"
        className={styles.linkButton}
        onClick={() => onViewTranscript(evidence.turnId, evidence.evidenceId)}
      >
        View in Transcript →
      </button>
    </div>
  );
}
