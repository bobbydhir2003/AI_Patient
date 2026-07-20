import type { CaseSection as CaseSectionData } from "../../types/case";
import { CaseCard } from "./CaseCard";
import { CaseSectionHeader } from "./CaseSectionHeader";
import styles from "./CaseCatalog.module.css";

/** Renders one catalog section generically - it knows no case ids in advance. */
export function CaseSection({ section }: { section: CaseSectionData }) {
  const advanced = section.id === "referral";
  return (
    <section
      className={`${styles.section} ${advanced ? styles.sectionAdvanced : ""}`}
      aria-label={section.title}
    >
      <CaseSectionHeader
        title={section.title}
        description={section.description}
        advanced={advanced}
      />
      <div className={styles.grid}>
        {section.cases.map((patientCase) => (
          <CaseCard key={patientCase.id} patientCase={patientCase} />
        ))}
      </div>
    </section>
  );
}
