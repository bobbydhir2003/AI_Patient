import type { PatientCase } from "../../types/case";

export const mockCases: PatientCase[] = [
  {
    id: "camden",
    caseCategory: "standard",
    name: "Camden",
    age: 4,
    image: "/patients/camden.webp",
    shortDescription:
      "A child receiving treatment for high-risk acute lymphoblastic leukemia who is experiencing reduced activity and difficulty keeping up with family activities.",
    referralReason:
      "Referred for a health-promotion interview to assess activity tolerance and participation during leukemia treatment.",
    studentVisibleInfo: [
      "Currently undergoing treatment for high-risk acute lymphoblastic leukemia.",
      "Caregiver reports reduced energy for play and family outings.",
      "Lives at home with two parents and an older sibling.",
    ],
    task:
      "Conduct a professional health-promotion interview to understand the patient's concerns, daily activities, participation, goals and relevant health behaviors.",
  },
  {
    id: "carly",
    caseCategory: "standard",
    name: "Carly",
    age: 38,
    image: "/patients/carly.webp",
    shortDescription:
      "An adult with a recent breast cancer diagnosis who is balancing treatment, family responsibilities, work demands and bilateral wrist pain.",
    referralReason:
      "Referred for a health-promotion interview to assess functional impact of wrist pain alongside cancer treatment demands.",
    studentVisibleInfo: [
      "Recently diagnosed with breast cancer and started treatment.",
      "Works full-time and is a primary caregiver for two children.",
      "Reports bilateral wrist pain affecting daily tasks.",
    ],
    task:
      "Conduct a professional health-promotion interview to understand the patient's concerns, daily activities, participation, goals and relevant health behaviors.",
  },
  {
    id: "sofia",
    caseCategory: "standard",
    name: "Sofia",
    age: 13,
    image: "/patients/sofia.webp",
    shortDescription:
      "A teenager with polyarticular juvenile idiopathic arthritis who is experiencing wrist and elbow discomfort, school limitations and reduced dance participation.",
    referralReason:
      "Referred for a health-promotion interview to assess impact of joint symptoms on school and dance activities.",
    studentVisibleInfo: [
      "Diagnosed with polyarticular juvenile idiopathic arthritis.",
      "Reports wrist and elbow discomfort, especially in the morning.",
      "Passionate about dance but has reduced participation recently.",
    ],
    task:
      "Conduct a professional health-promotion interview to understand the patient's concerns, daily activities, participation, goals and relevant health behaviors.",
  },
  {
    id: "jayden",
    caseCategory: "standard",
    name: "Jayden",
    age: 45,
    image: "/patients/jayden.webp",
    shortDescription:
      "An active adult recently diagnosed with systemic lupus erythematosus who wants to continue exercising, participating with family and leading a running program.",
    referralReason:
      "Referred for a health-promotion interview to assess goals for continued exercise and running participation.",
    studentVisibleInfo: [
      "Recently diagnosed with systemic lupus erythematosus.",
      "Previously led a community running program.",
      "Motivated to stay active despite new diagnosis.",
    ],
    task:
      "Conduct a professional health-promotion interview to understand the patient's concerns, daily activities, participation, goals and relevant health behaviors.",
  },
];

export function getCaseById(caseId: string): PatientCase | undefined {
  return mockCases.find((c) => c.id === caseId);
}
