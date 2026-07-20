import styles from "./assessment.module.css";

export function AssessmentStatus({
  phase,
  friendlyMessage,
  onRetry,
  onBack,
}: {
  phase: "processing" | "failed";
  /** A structured, student-safe backend message. Raw transport errors
   * (e.g. "Request failed with status 404") must stay in the console only. */
  friendlyMessage?: string | null;
  onRetry: () => void;
  onBack?: () => void;
}) {
  if (phase === "processing") {
    return (
      <div className={`card ${styles.statusWrap}`} role="status" aria-live="polite">
        <div className={styles.spinner} aria-hidden="true" />
        <h2 className={styles.sectionTitle}>Generating your AI assessment...</h2>
        <p className={styles.mutedText}>
          The AI is reading your complete transcript, extracting evidence for each rubric,
          evaluating performance, and verifying the results. This can take a minute.
        </p>
      </div>
    );
  }
  return (
    <div className={`card ${styles.statusWrap}`} role="alert">
      <h2 className={styles.sectionTitle}>Assessment could not be loaded</h2>
      <p className={styles.mutedText}>
        {friendlyMessage ??
          "We could not find an assessment for this completed interview. Your transcript is saved - generate the assessment again, or return to case selection."}
      </p>
      <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", justifyContent: "center" }}>
        <button type="button" className="btn btn-primary" onClick={onRetry}>
          Generate Assessment Again
        </button>
        {onBack && (
          <button type="button" className="btn btn-secondary" onClick={onBack}>
            Back to Case Selection
          </button>
        )}
      </div>
    </div>
  );
}
