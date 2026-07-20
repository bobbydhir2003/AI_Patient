import { useEffect, useMemo, useRef, useState } from "react";
import type { AssessmentTurn, TranscriptMarker } from "../../types/assessment";
import styles from "./assessment.module.css";

const TYPE_LABEL: Record<string, string> = {
  strength: "Strength",
  missed_opportunity: "Missed Opportunity",
  mistake: "Mistake",
  safety_concern: "Safety Concern",
  observation: "Observation",
};

const CHIP_CLASS: Record<string, string> = {
  strength: styles.chipStrength,
  missed_opportunity: styles.chipMissed,
  mistake: styles.chipMistake,
  safety_concern: styles.chipSafety,
  observation: styles.chipObservation,
};

export function TranscriptReview({
  turns,
  rubricDomains,
  targetTurnId,
  targetEvidenceId,
}: {
  turns: AssessmentTurn[];
  rubricDomains: string[];
  targetTurnId: string | null;
  targetEvidenceId: string | null;
}) {
  const [rubricFilter, setRubricFilter] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [openMarker, setOpenMarker] = useState<{ turnId: string; marker: TranscriptMarker } | null>(null);
  const turnRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    if (targetTurnId) {
      turnRefs.current[targetTurnId]?.scrollIntoView({ behavior: "smooth", block: "center" });
      if (targetEvidenceId) {
        const turn = turns.find((t) => t.turnId === targetTurnId);
        const marker = turn?.markers.find((m) => m.evidenceId === targetEvidenceId);
        if (turn && marker) setOpenMarker({ turnId: turn.turnId, marker });
      }
    }
  }, [targetTurnId, targetEvidenceId, turns]);

  const markerMatches = (marker: TranscriptMarker) =>
    (!rubricFilter || marker.rubricDomain === rubricFilter) &&
    (!typeFilter || marker.evidenceType === typeFilter);

  const visibleTurns = useMemo(() => {
    if (!rubricFilter && !typeFilter) return turns;
    // keep full surrounding context: show all turns, but only matching markers
    return turns;
  }, [turns, rubricFilter, typeFilter]);

  return (
    <div className={styles.transcriptWrap}>
      <div className={styles.filterRow}>
        <button
          type="button"
          className={`${styles.filterButton} ${!rubricFilter ? styles.filterActive : ""}`}
          onClick={() => setRubricFilter(null)}
        >
          All rubrics
        </button>
        {rubricDomains.map((domain) => (
          <button
            key={domain}
            type="button"
            className={`${styles.filterButton} ${rubricFilter === domain ? styles.filterActive : ""}`}
            onClick={() => setRubricFilter((f) => (f === domain ? null : domain))}
          >
            {domain}
          </button>
        ))}
      </div>
      <div className={styles.filterRow}>
        {Object.entries(TYPE_LABEL).map(([type, label]) => (
          <button
            key={type}
            type="button"
            className={`${styles.filterButton} ${typeFilter === type ? styles.filterActive : ""}`}
            onClick={() => setTypeFilter((f) => (f === type ? null : type))}
          >
            {label}
          </button>
        ))}
      </div>

      {visibleTurns.map((turn) => {
        const markers = turn.markers.filter(markerMatches);
        const highlighted = turn.turnId === targetTurnId;
        return (
          <div
            key={turn.turnId}
            ref={(el) => {
              turnRefs.current[turn.turnId] = el;
            }}
            className={`${styles.turnRow} ${highlighted ? styles.turnHighlighted : ""}`}
          >
            <div className={styles.turnHeader}>
              <span
                className={`${styles.turnSpeaker} ${
                  turn.sender === "student" ? styles.speakerStudent : styles.speakerPatient
                }`}
              >
                {turn.sender === "student" ? "YOU" : "PATIENT"}
              </span>
              <span className={styles.turnLabelTag}>{turn.turnLabel}</span>
            </div>
            <p className={styles.turnText}>{turn.text}</p>
            {markers.length > 0 && (
              <div className={styles.markerRow}>
                {markers.map((marker) => (
                  <button
                    key={marker.evidenceId}
                    type="button"
                    className={`${styles.markerChip} ${CHIP_CLASS[marker.evidenceType] ?? ""}`}
                    onClick={() =>
                      setOpenMarker((current) =>
                        current?.marker.evidenceId === marker.evidenceId
                          ? null
                          : { turnId: turn.turnId, marker },
                      )
                    }
                  >
                    {TYPE_LABEL[marker.evidenceType]}: {marker.label}
                  </button>
                ))}
              </div>
            )}
            {openMarker?.turnId === turn.turnId && (
              <div className={styles.markerPanel}>
                <div className={styles.evidenceTop}>
                  <span className={styles.evidenceLabel}>{openMarker.marker.label}</span>
                  <span className={styles.confidenceTag}>
                    {openMarker.marker.rubricDomain} ·{" "}
                    {openMarker.marker.confidenceLevel === "strong"
                      ? "Strong transcript evidence"
                      : openMarker.marker.confidenceLevel === "moderate"
                        ? "Moderate transcript evidence"
                        : "Insufficient evidence"}
                    {openMarker.marker.reviewerConfirmed ? " · Confirmed by AI reviewer" : ""}
                  </span>
                </div>
                <p className={styles.mutedText}>{openMarker.marker.explanation}</p>
                {openMarker.marker.whyItMatters && (
                  <p className={styles.mutedText}>
                    <strong>Why it matters:</strong> {openMarker.marker.whyItMatters}
                  </p>
                )}
                {openMarker.marker.suggestedAlternative && (
                  <p className={`${styles.mutedText} ${styles.momentAlt}`}>
                    <strong>Try instead:</strong> &ldquo;{openMarker.marker.suggestedAlternative}&rdquo;
                  </p>
                )}
                <button type="button" className={styles.linkButton} onClick={() => setOpenMarker(null)}>
                  Close
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
