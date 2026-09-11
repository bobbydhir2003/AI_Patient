import { LIKERT_OPTIONS } from "../../services/surveyQuestions";
import styles from "./SurveyLikert.module.css";

interface SurveyLikertProps {
  /** Exact REDCap variable name — used as the radio group name. */
  name: string;
  index: number;
  prompt: string;
  value: number | undefined;
  onChange: (value: number) => void;
  disabled?: boolean;
}

/** One 1–5 Likert row: prompt + five selectable options (native radios styled
 * as segments, so keyboard/screen-reader behaviour is preserved). */
export function SurveyLikert({
  name,
  index,
  prompt,
  value,
  onChange,
  disabled = false,
}: SurveyLikertProps) {
  return (
    <fieldset
      className={`${styles.item} ${disabled ? styles.itemDisabled : ""}`}
      disabled={disabled}
    >
      <legend className={styles.prompt}>
        <span className={styles.num}>{index}.</span> {prompt}
      </legend>
      <div className={styles.scale}>
        {LIKERT_OPTIONS.map((opt) => {
          const id = `${name}-${opt.value}`;
          const active = value === opt.value;
          return (
            <label
              key={opt.value}
              htmlFor={id}
              className={`${styles.option} ${active ? styles.optionActive : ""}`}
            >
              <input
                id={id}
                type="radio"
                name={name}
                className={styles.radio}
                checked={active}
                onChange={() => {
                  if (!disabled) onChange(opt.value);
                }}
              />
              <span className={styles.optValue}>{opt.value}</span>
              <span className={styles.optLabel}>{opt.label}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
