import styles from "./CaseCatalog.module.css";

export function CaseDifficultyBadge({ difficulty }: { difficulty?: string }) {
  if (!difficulty) return null;
  const advanced = difficulty.toLowerCase() === "advanced";
  return (
    <span className={`${styles.difficultyBadge} ${advanced ? styles.difficultyAdvanced : ""}`}>
      {difficulty}
    </span>
  );
}
