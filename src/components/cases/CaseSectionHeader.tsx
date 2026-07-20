import styles from "./CaseCatalog.module.css";

export function CaseSectionHeader({
  title,
  description,
  advanced,
}: {
  title: string;
  description: string;
  advanced?: boolean;
}) {
  return (
    <div className={styles.sectionHeader}>
      <div className={styles.sectionTitleRow}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        {advanced && <span className={styles.advancedTag}>Advanced</span>}
      </div>
      <p className={styles.sectionDescription}>{description}</p>
    </div>
  );
}
