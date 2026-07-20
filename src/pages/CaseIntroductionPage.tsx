import { useNavigate, useParams } from "react-router-dom";
import { PatientProfile } from "../components/cases/PatientProfile";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { usePatientCase } from "../services/cases";
import styles from "./CaseIntroductionPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Complete"];

export function CaseIntroductionPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const { patientCase, loading, error, retry } = usePatientCase(caseId);

  if (loading && !patientCase) {
    return (
      <div className="page">
        <p role="status">Loading patient case...</p>
      </div>
    );
  }

  if (error && !patientCase) {
    return (
      <div className="page">
        <div className={`card ${styles.notFound}`} role="alert">
          <p>{error}</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={retry}
            style={{ marginTop: "1rem" }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!patientCase) {
    return (
      <div className="page">
        <div className={`card ${styles.notFound}`}>
          <p>We couldn't find that patient case.</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => navigate("/cases")}
            style={{ marginTop: "1rem" }}
          >
            Back to Case Selection
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={0} />

      <div className={styles.layout}>
        <div className="card" style={{ padding: "1.5rem" }}>
          <PatientProfile patientCase={patientCase} />
        </div>

        <div className={styles.infoGrid}>
          <div className={`card ${styles.section}`}>
            <h2 className={styles.sectionTitle}>Referral Reason</h2>
            <p className={styles.sectionBody}>{patientCase.referralReason}</p>
          </div>

          <div className={`card ${styles.section}`}>
            <h2 className={styles.sectionTitle}>Student-Visible Information</h2>
            <ul className={styles.infoList}>
              {patientCase.studentVisibleInfo.map((info) => (
                <li key={info}>{info}</li>
              ))}
            </ul>
          </div>
        </div>

        <div className={`card ${styles.taskPanel}`}>
          <h2 className={styles.sectionTitle}>Your Task</h2>
          <p className={styles.sectionBody}>{patientCase.task}</p>
        </div>

        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => navigate("/cases")}
          >
            Back
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => navigate(`/interview/${patientCase.id}`)}
          >
            Start Interview
          </button>
        </div>
      </div>
    </div>
  );
}
