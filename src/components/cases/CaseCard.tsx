import { useNavigate } from "react-router-dom";
import type { PatientCase } from "../../types/case";
import { AppImage } from "../common/AppImage";
import styles from "./CaseCard.module.css";

interface CaseCardProps {
  patientCase: PatientCase;
}

/** One card for ANY case (standard or referral) - fully data-driven. */
export function CaseCard({ patientCase }: CaseCardProps) {
  const navigate = useNavigate();

  return (
    <article className={`card ${styles.card}`}>
      <AppImage
        src={patientCase.image}
        alt={`${patientCase.name} patient portrait`}
        className={styles.image}
      />
      <div className={styles.body}>
        {patientCase.setting && (
          <div className={styles.metaRow}>
            <span className={styles.settingBadge}>{patientCase.setting}</span>
          </div>
        )}
        <h3 className={styles.name}>{patientCase.name}</h3>
        <p className={styles.age}>Age: {patientCase.age}</p>
        <p className={styles.description}>{patientCase.shortDescription}</p>
        <button
          type="button"
          className={styles.startButton}
          onClick={() => navigate(`/cases/${patientCase.id}`)}
        >
          <span>Start Interview</span>
          <span className={styles.buttonArrow} aria-hidden="true">›</span>
        </button>
      </div>
    </article>
  );
}
