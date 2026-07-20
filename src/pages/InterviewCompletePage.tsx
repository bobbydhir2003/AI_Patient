import { useNavigate } from "react-router-dom";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import styles from "./InterviewCompletePage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Complete"];

/**
 * Shown after an interview is completed and locked. The AI assessment system
 * does not exist yet, so no scores or fake processing stages are displayed.
 */
export function InterviewCompletePage() {
  const navigate = useNavigate();

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={2} />
      <div className={styles.wrapper}>
        <div className={`card ${styles.cardBody}`}>
          <div className={styles.icon} aria-hidden="true">
            ✓
          </div>
          <h1 className={styles.heading}>Interview Complete</h1>
          <p className={styles.message}>Your interview has been saved.</p>
          <div className={styles.actions}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => navigate("/cases")}
            >
              Try Another Case
            </button>
            <button type="button" className="btn btn-primary" onClick={() => navigate("/")}>
              Return Home
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
