import { CaseSection } from "../components/cases/CaseSection";
import { useCaseCatalog } from "../services/cases";
import styles from "./CaseSelectionPage.module.css";

/**
 * Case library: renders whatever sections the backend catalog returns.
 * No case ids or section contents are known to the frontend in advance -
 * adding a new case only requires registering it in the backend catalog.
 */
export function CaseCatalogPage() {
  const { catalog, loading, error, retry } = useCaseCatalog();

  return (
    <div className="page">
      <div className={styles.header}>
        <h1 className={styles.title}>Choose a Simulation</h1>
        <p className={styles.subtitle}>Select a patient to begin your interview.</p>
      </div>

      {loading ? (
        <p role="status">Loading patient cases...</p>
      ) : error ? (
        <div className="card" role="alert" style={{ padding: "1.5rem", maxWidth: "480px" }}>
          <p style={{ marginBottom: "1rem" }}>{error}</p>
          <button type="button" className="btn btn-primary" onClick={retry}>
            Retry
          </button>
        </div>
      ) : !catalog || catalog.sections.length === 0 ? (
        <p role="status">No cases are available yet.</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
          {catalog.sections.map((section) => (
            <CaseSection key={section.id} section={section} />
          ))}
        </div>
      )}
    </div>
  );
}
