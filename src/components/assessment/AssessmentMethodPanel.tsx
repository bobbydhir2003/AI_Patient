import type { Assessment } from "../../types/assessment";
import styles from "./assessment.module.css";

const STEPS: Array<{ title: string; text: string }> = [
  { title: "Interview Transcript", text: "The complete saved conversation between you and the patient was reviewed." },
  { title: "Case Knowledge Base", text: "The AI used case-specific expectations for this patient, including safety considerations and patient context." },
  { title: "Rubric Framework", text: "Evaluation is based on four clinical communication rubrics defined by PT educators." },
  { title: "AI Evidence Extraction", text: "The AI analyzed the transcript and extracted evidence moments for each rubric domain." },
  { title: "AI Rubric Evaluation", text: "The AI judged performance for each domain from the transcript and evidence — no keyword scoring or fixed answer matching is used." },
  { title: "Quality Review & Validation", text: "A second AI review checked that every feedback item is supported by transcript evidence and is fair and consistent." },
];

export function AssessmentMethodPanel({ assessment }: { assessment: Assessment }) {
  return (
    <div className={`card ${styles.sectionCard}`}>
      <h2 className={styles.sectionTitle}>How This Assessment Was Built</h2>
      {STEPS.map((step, index) => (
        <div key={step.title} className={styles.methodStep}>
          <span className={styles.methodNum}>{index + 1}</span>
          <div>
            <p className={styles.methodTitle}>{step.title}</p>
            <p className={styles.methodText}>{step.text}</p>
          </div>
        </div>
      ))}
      <div className={styles.metaGrid}>
        <div className={styles.metaRow}><span className={styles.metaKey}>Assessment type</span><span>AI-generated formative review</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Model</span><span>{assessment.modelName}</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Case version</span><span>{assessment.caseVersion}</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Rubric version</span><span>{assessment.rubricVersion}</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Prompt version</span><span>{assessment.promptVersion}</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Verification</span><span>{assessment.verificationStatus ?? "—"}</span></div>
        <div className={styles.metaRow}><span className={styles.metaKey}>Generated</span><span>{assessment.completedAt ? new Date(assessment.completedAt).toLocaleString() : "—"}</span></div>
      </div>
      <p className={styles.methodText}>
        AI assessment may make mistakes. Review the linked transcript evidence and discuss
        disputed feedback with an instructor.
      </p>
    </div>
  );
}
