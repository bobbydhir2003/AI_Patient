import { useNavigate, useParams } from "react-router-dom";
import { AppImage } from "../components/common/AppImage";
import { PavingWheel } from "../components/cases/PavingWheel";
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

  const {
    name,
    age,
    image,
    patientType,
    gender,
    raceEthnicity,
    medicalHistory,
    medications,
    referralReason,
    studentVisibleInfo,
    caregiverNotice,
    pavingProfile,
    task,
  } = patientCase;

  const genderLabel = gender
    ? gender.charAt(0).toUpperCase() + gender.slice(1)
    : "";
  const hasMeds = Array.isArray(medications) && medications.length > 0;
  const hasClinical = Boolean(medicalHistory) || hasMeds;

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={0} />

      <div className={styles.layout}>
        {/* SECTION 1: Patient header */}
        <div className={`card ${styles.header}`}>
          <div className={styles.identity}>
            <AppImage
              src={image}
              alt={`${name} patient portrait`}
              className={styles.portrait}
            />
            <div className={styles.identityDetails}>
              <div className={styles.nameRow}>
                <h1 className={styles.name}>{name}</h1>
                {patientType && (
                  <span className={styles.typeBadge}>{patientType}</span>
                )}
              </div>
              <dl className={styles.metaGrid}>
                <div className={styles.metaItem}>
                  <dt>Age</dt>
                  <dd>{age === 1 ? "1 year" : `${age} years`}</dd>
                </div>
                {genderLabel && (
                  <div className={styles.metaItem}>
                    <dt>Gender</dt>
                    <dd>{genderLabel}</dd>
                  </div>
                )}
                {raceEthnicity && (
                  <div className={styles.metaItem}>
                    <dt>Race / Ethnicity</dt>
                    <dd>{raceEthnicity}</dd>
                  </div>
                )}
              </dl>
            </div>
          </div>

          {hasClinical && (
            <div className={styles.clinical}>
              {medicalHistory && (
                <div className={styles.clinicalBlock}>
                  <h2 className={styles.clinicalTitle}>Medical History</h2>
                  <p className={styles.clinicalBody}>{medicalHistory}</p>
                </div>
              )}
              {hasMeds && (
                <div className={styles.clinicalBlock}>
                  <h2 className={styles.clinicalTitle}>Medications</h2>
                  <ul className={styles.medList}>
                    {medications!.map((med) => (
                      <li key={med}>{med}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Caregiver notice (e.g. Camden's mother answers) */}
        {caregiverNotice && (
          <div className={styles.notice} role="note">
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
              className={styles.noticeIcon}
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="16" x2="12" y2="12" />
              <line x1="12" y1="8" x2="12.01" y2="8" />
            </svg>
            <p>{caregiverNotice}</p>
          </div>
        )}

        {/* SECTION 2 & 3: Referral Reason + Student-Visible Information */}
        <div className={styles.infoGrid}>
          <div className={`card ${styles.section}`}>
            <h2 className={styles.sectionTitle}>Referral Reason</h2>
            <p className={styles.sectionBody}>{referralReason}</p>
          </div>

          <div className={`card ${styles.section}`}>
            <h2 className={styles.sectionTitle}>Student-Visible Information</h2>
            <ul className={styles.infoList}>
              {studentVisibleInfo.map((info) => (
                <li key={info}>{info}</li>
              ))}
            </ul>
          </div>
        </div>

        {/* SECTION 4: PAVING Wheel (digital radar from the patient's worksheet) */}
        {pavingProfile && pavingProfile.categories.length > 0 && (
          <PavingWheel patientName={name} profile={pavingProfile} />
        )}

        {/* SECTION 5: Your Task */}
        <div className={`card ${styles.taskPanel}`}>
          <h2 className={styles.sectionTitle}>Your Task</h2>
          <p className={styles.sectionBody}>{task}</p>
        </div>

        {/* Navigation */}
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
