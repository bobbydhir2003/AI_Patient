import type { PavingProfile } from "../../types/case";

/**
 * Fixed, patient-INDEPENDENT PAVING example used ONLY by the info modal.
 * It never reads case data and is identical for every case (Camden, Carly,
 * Sofia, Jayden). Same 12 categories, order, colors and 0-25 scale as the real
 * wheel, with a neutral balanced shape (both higher and lower example points)
 * that does not match any real patient result.
 */
const CATEGORIES: { key: string; label: string; color: string; value: number }[] = [
  { key: "physical_activity", label: "Physical Activity", color: "#5cc8ff", value: 22 },
  { key: "attitude", label: "Attitude", color: "#f5923e", value: 20 },
  { key: "variety", label: "Variety", color: "#8ecb3a", value: 19 },
  { key: "investigations", label: "Investigations", color: "#b79ce8", value: 18 },
  { key: "nutrition", label: "Nutrition", color: "#ef4f8b", value: 15 },
  { key: "goals", label: "Goals", color: "#f2c744", value: 16 },
  { key: "stress_management", label: "Stress Management", color: "#46cfe0", value: 14 },
  { key: "time_outs", label: "Time Outs", color: "#f5923e", value: 17 },
  { key: "energy", label: "Energy", color: "#8ecb3a", value: 18 },
  { key: "purpose", label: "Purpose", color: "#b79ce8", value: 19 },
  { key: "sleep", label: "Sleep", color: "#ef4f8b", value: 20 },
  { key: "social_connections", label: "Social Connections", color: "#f2c744", value: 21 },
];

export const DEFAULT_PAVING_EXAMPLE: PavingProfile = {
  sourceFile: "",
  sourcePage: null,
  maxValue: 25,
  needsReview: [],
  categories: CATEGORIES.map((c) => ({
    key: c.key,
    label: c.label,
    value: c.value,
    maxValue: 25,
    labelColor: c.color,
  })),
};
