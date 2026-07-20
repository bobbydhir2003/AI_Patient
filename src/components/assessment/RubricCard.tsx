import { useState } from "react";
import type { DomainResult, Rubric } from "../../types/assessment";
import { LevelBadge } from "./LevelBadge";
import { ReviewThisMoment } from "./ReviewThisMoment";
import styles from "./assessment.module.css";

const TYPE_CLASS: Record<string, string> = {
  strength: styles.evidenceStrength,
  missed_opportunity: styles.evidenceMissed,
  mistake: styles.evidenceMistake,
  safety_concern: styles.evidenceSafety,
};

const CONFIDENCE_TEXT: Record<string, string> = {
  strong: "Strong transcript evidence",
  moderate: "Moderate transcript evidence",
  insufficient: "Insufficient evidence",
};

export function RubricCard({
  domain,
  rubric,
  onViewTranscript,
}: {
  domain: DomainResult;
  rubric: Rubric | undefined;
  onViewTranscript: (turnId: string, evidenceId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const strengths = domain.evidence.filter((e) => e.evidenceType === "strength");
  const problems = domain.evidence.filter((e) => e.evidenceType !== "strength");

  return (
    <div className={`card ${styles.rubricCard}`}>
      <button
        type="button"
        className={styles.rubricHeader}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className={styles.rubricTitleWrap}>
          <span className={styles.rubricTitle}>{domain.rubricDomain}</span>
          <span className={styles.rubricSummary}>{domain.summary}</span>
        </span>
        <span className={styles.rubricMeta}>
          <LevelBadge level={domain.performanceLevel} />
          <span className={styles.evidenceCount}>
            {domain.evidence.length} transcript moment{domain.evidence.length === 1 ? "" : "s"}
          </span>
          <span className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`} aria-hidden="true">
            ›
          </span>
        </span>
      </button>

      {open && (
        <div className={styles.rubricBody}>
          {rubric && (
            <div className={styles.listBlock}>
              <span className={styles.listHeading}>What this rubric evaluates</span>
              <p className={styles.mutedText}>{rubric.studentFacingDescription}</p>
            </div>
          )}

          {domain.narrative && (
            <div className={styles.listBlock}>
              <span className={styles.listHeading}>AI judgment</span>
              <p className={styles.mutedText}>{domain.narrative}</p>
            </div>
          )}

          {domain.strengths.length > 0 && (
            <div className={styles.listBlock}>
              <span className={`${styles.listHeading} ${styles.headingStrength}`}>What went well</span>
              <ul className={styles.plainList}>
                {domain.strengths.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {domain.areasForGrowth.length > 0 && (
            <div className={styles.listBlock}>
              <span className={`${styles.listHeading} ${styles.headingGrowth}`}>Growth opportunities</span>
              <ul className={styles.plainList}>
                {domain.areasForGrowth.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {strengths.length > 0 && (
            <div className={styles.listBlock}>
              <span className={`${styles.listHeading} ${styles.headingStrength}`}>
                Transcript evidence — strengths
              </span>
              {strengths.map((e) => (
                <div key={e.evidenceId} className={`${styles.evidenceItem} ${TYPE_CLASS[e.evidenceType] ?? ""}`}>
                  <div className={styles.evidenceTop}>
                    <span className={styles.evidenceLabel}>{e.label}</span>
                    <span className={styles.confidenceTag}>
                      {CONFIDENCE_TEXT[e.confidenceLevel]} · {e.turnLabel}
                    </span>
                  </div>
                  {e.studentExcerpt && (
                    <p className={styles.evidenceQuote}>
                      <span className={styles.quoteWho}>YOU</span>&ldquo;{e.studentExcerpt}&rdquo;
                    </p>
                  )}
                  {e.patientExcerpt && (
                    <p className={styles.evidenceQuote}>
                      <span className={styles.quoteWho}>PATIENT</span>&ldquo;{e.patientExcerpt}&rdquo;
                    </p>
                  )}
                  <p className={styles.mutedText}>{e.explanation}</p>
                  <button
                    type="button"
                    className={styles.linkButton}
                    onClick={() => onViewTranscript(e.turnId, e.evidenceId)}
                  >
                    View in Transcript →
                  </button>
                </div>
              ))}
            </div>
          )}

          {problems.length > 0 && (
            <div className={styles.listBlock}>
              <span className={`${styles.listHeading} ${styles.headingGrowth}`}>
                Mistakes & missed opportunities
              </span>
              {problems.map((e) => (
                <ReviewThisMoment
                  key={e.evidenceId}
                  evidence={e}
                  rubricDomain={domain.rubricDomain}
                  onViewTranscript={onViewTranscript}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
