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
