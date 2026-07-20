import type { Assessment } from "../../types/assessment";
import { LevelBadge } from "./LevelBadge";
import styles from "./assessment.module.css";

export function OverallImpression({ assessment }: { assessment: Assessment }) {
  return (
    <div className={`card ${styles.sectionCard}`}>
      <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
        <h2 className={styles.sectionTitle}>Overall AI Impression</h2>
        <span className={styles.aiTag}>AI Generated</span>
        <LevelBadge level={assessment.overallLevel} />
      </div>
      <p className={styles.mutedText}>{assessment.overallSummary}</p>
    </div>
  );
}
