import type { PatientCase } from "../../types/case";
import { AppImage } from "../common/AppImage";
import styles from "./PatientProfile.module.css";

interface PatientProfileProps {
  patientCase: PatientCase;
  size?: "medium" | "large";
}

export function PatientProfile({ patientCase, size = "medium" }: PatientProfileProps) {
  const imageClassName = `${styles.image} ${size === "large" ? styles.imageLarge : ""}`;

  return (
    <div className={styles.profile}>
      <AppImage
        src={patientCase.image}
        alt={`${patientCase.name} patient portrait`}
        className={imageClassName}
      />
      <div className={styles.details}>
        <h2 className={styles.name}>{patientCase.name}</h2>
        <p className={styles.age}>Age: {patientCase.age}</p>
      </div>
    </div>
  );
}
