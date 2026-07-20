import { useNavigate } from "react-router-dom";
import type { PatientCase } from "../../types/case";
import { AppImage } from "../common/AppImage";
import { CaseDifficultyBadge } from "./CaseDifficultyBadge";
import styles from "./CaseCard.module.css";
import catalogStyles from "./CaseCatalog.module.css";

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
      <h3 className={styles.name}>{patientCase.name}</h3>
      <p className={styles.age}>Age: {patientCase.age}</p>
      <p className={styles.description}>{patientCase.shortDescription}</p>
      {(patientCase.setting || patientCase.difficulty || patientCase.estimatedMinutes) && (
        <div className={catalogStyles.cardMeta}>
          <div className={catalogStyles.cardMetaRow}>
            <CaseDifficultyBadge difficulty={patientCase.difficulty} />
            {patientCase.estimatedMinutes ? <span>~{patientCase.estimatedMinutes} min</span> : null}
          </div>
          {patientCase.setting && <span>{patientCase.setting}</span>}
        </div>
      )}
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => navigate(`/cases/${patientCase.id}`)}
      >
        Start Case
      </button>
    </article>
  );
}
