import type { FocusArea } from "../../types/assessment";
import styles from "./assessment.module.css";

export function FocusAreas({ areas }: { areas: FocusArea[] }) {
  if (!areas.length) return null;
  return (
    <div className={`card ${styles.sectionCard}`}>
      <h2 className={styles.sectionTitle}>Priority Improvement Opportunities</h2>
      <p className={styles.mutedText}>Focus on these areas to strengthen your next interview.</p>
      {areas.map((area, index) => (
        <div key={area.title} className={styles.focusItem}>
          <span className={styles.focusNum}>{index + 1}</span>
          <div>
            <p className={styles.evidenceLabel}>{area.title}</p>
            <p className={styles.mutedText}>
              <strong>Why:</strong> {area.whyItMatters}
            </p>
            {area.suggestedPractice && (
              <p className={styles.mutedText}>
                <strong>Practice:</strong> {area.suggestedPractice}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
