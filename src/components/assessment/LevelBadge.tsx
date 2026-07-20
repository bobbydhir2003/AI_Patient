import type { PerformanceLevel } from "../../types/assessment";
import styles from "./assessment.module.css";

const LEVEL_CLASS: Record<PerformanceLevel, string> = {
  Advanced: styles.levelAdvanced,
  Proficient: styles.levelProficient,
  Developing: styles.levelDeveloping,
  "Needs Improvement": styles.levelNeedsImprovement,
  "Insufficient Evidence": styles.levelInsufficientEvidence,
};

export function LevelBadge({ level }: { level: PerformanceLevel | null }) {
  if (!level) return null;
  return <span className={`${styles.levelBadge} ${LEVEL_CLASS[level] ?? ""}`}>{level}</span>;
}
