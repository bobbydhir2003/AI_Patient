import styles from "./ProgressSteps.module.css";

interface ProgressStepsProps {
  steps: string[];
  currentStepIndex: number;
}

export function ProgressSteps({ steps, currentStepIndex }: ProgressStepsProps) {
  return (
    <ol className={styles.list} aria-label="Interview progress">
      {steps.map((step, index) => {
        const isActive = index === currentStepIndex;
        const isComplete = index < currentStepIndex;
        const stateClass = isActive
          ? styles.active
          : isComplete
          ? styles.complete
          : "";
        return (
          <li key={step} style={{ display: "contents" }}>
            <span
              className={`${styles.step} ${stateClass}`}
              aria-current={isActive ? "step" : undefined}
            >
              <span className={styles.bullet}>
                {isComplete ? "✓" : index + 1}
              </span>
              <span>{step}</span>
            </span>
            {index < steps.length - 1 && <span className={styles.connector} />}
          </li>
        );
      })}
    </ol>
  );
}
