import styles from "./assessment.module.css";

export type AssessmentTab = "overview" | "rubrics" | "transcript" | "method";

const TABS: Array<{ id: AssessmentTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "rubrics", label: "Rubric Review" },
  { id: "transcript", label: "Transcript Review" },
  { id: "method", label: "How This Assessment Works" },
];

export function AssessmentHeader({
  tab,
  onTabChange,
}: {
  tab: AssessmentTab;
  onTabChange: (tab: AssessmentTab) => void;
}) {
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
        <h1 style={{ fontSize: "1.4rem", fontWeight: 800 }}>AI Assessment Review</h1>
        <span className={styles.aiTag}>AI Generated</span>
      </div>
      <p className={styles.mutedText} style={{ marginTop: "6px" }}>
        AI-generated formative review based on your completed patient interview, the selected
        patient case, and four instructor-defined rubrics.
      </p>
      <div className={styles.filterRow} style={{ marginTop: "16px" }} role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`${styles.filterButton} ${tab === t.id ? styles.filterActive : ""}`}
            onClick={() => onTabChange(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
    </div>
  );
}
