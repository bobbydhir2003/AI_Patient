export interface PatientCase {
  id: string;
  caseCategory: "standard" | "referral";
  name: string;
  age: number;
  image: string;
  shortDescription: string;
  referralReason: string;
  studentVisibleInfo: string[];
  task: string;
  setting?: string;
  difficulty?: string;
  estimatedMinutes?: number | null;
  // Optional student-safe presentation fields (Case Introduction screen).
  // Populated for standard cases only; safe to be absent/empty otherwise.
  gender?: string;
  raceEthnicity?: string;
  patientType?: string;
  medicalHistory?: string;
  medications?: string[];
  pavingWheelImage?: string;
  caregiverNotice?: string;
  primarySpeaker?: string;
  pavingProfile?: PavingProfile;
}

export interface PavingCategory {
  key: string;
  label: string;
  /** Real value from the patient's worksheet; null when the source total was
   * illegible (needs review) - never fabricated. */
  value: number | null;
  maxValue: number;
  labelColor: string;
}

export interface PavingProfile {
  sourceFile: string;
  sourcePage?: number | null;
  maxValue: number;
  categories: PavingCategory[];
  needsReview: string[];
}

export interface CaseSection {
  id: string;
  title: string;
  description: string;
  cases: PatientCase[];
}

export interface CaseCatalog {
  sections: CaseSection[];
}
