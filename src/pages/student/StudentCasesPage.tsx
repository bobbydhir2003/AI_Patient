import { useNavigate } from "react-router-dom";
import { useCaseCatalog } from "../../services/cases";
import { CaseCard } from "../../components/cases/CaseCard";
import { AppImage } from "../../components/common/AppImage";
import { ErrorState, Spinner } from "../../portal/ui";
import styles from "./StudentDashboardPage.module.css";

/** Full patient-case library for the student app (bottom-nav "Cases"). Reuses
 * the real backend catalog + the shared CaseCard; no case data is hard-coded. */
export function StudentCasesPage() {
  const navigate = useNavigate();
  const { catalog, loading, error, retry } = useCaseCatalog();
  const standard = catalog?.sections.find((s) => s.id === "standard");
  const referral = catalog?.sections.find((s) => s.id === "referral");

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.welcome}>Patient Cases</h1>
          <p className={styles.welcomeSub}>Practice real-world scenarios and build your clinical communication skills.</p>
        </div>
      </div>

      {loading && <Spinner label="Loading patient cases…" />}
      {error && <ErrorState message={error} onRetry={retry} />}

      {standard && (
        <div className={styles.caseGrid}>
          {standard.cases.map((c) => <CaseCard key={c.id} patientCase={c} />)}
        </div>
      )}

      {referral && referral.cases.length > 0 && (
        <div style={{ marginTop: 28 }}>
          <div className={styles.sectionHead}>
            <div>
              <h2 className={styles.sectionTitle}>
                Referral &amp; Interprofessional Cases <span className={styles.advanced}>ADVANCED</span>
              </h2>
              <p className={styles.sectionSub}>{referral.description}</p>
            </div>
          </div>
          <div className={styles.referralGrid}>
            {referral.cases.map((c) => (
              <button key={c.id} type="button" className={styles.referralRow} onClick={() => navigate(`/cases/${c.id}`)}>
                <AppImage src={c.image} alt={`${c.name} patient portrait`} className={styles.referralThumb} />
                <span>
                  <span className={styles.referralName}>{c.name}</span>
                  <span className={styles.referralMeta}>{c.setting || "Referral case"}</span>
                </span>
                <span className={styles.chevron}>›</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
