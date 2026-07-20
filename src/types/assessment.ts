/** Assessment API types (mirror backend camelCase schemas). */

export type PerformanceLevel =
  | "Advanced"
  | "Proficient"
  | "Developing"
  | "Needs Improvement"
  | "Insufficient Evidence";

export type EvidenceType =
  | "strength"
  | "missed_opportunity"
  | "mistake"
  | "safety_concern"
  | "observation";

export interface AssessmentEvidence {
  evidenceId: string;
  turnId: string;
  turnLabel: string;
  evidenceType: EvidenceType;
  label: string;
  severity: string | null;
  studentExcerpt: string;
  patientExcerpt: string;
  explanation: string;
  whyItMatters: string;
  suggestedAlternative: string;
  confidenceLevel: "strong" | "moderate" | "insufficient";
  reviewerConfirmed: boolean;
}

export interface DomainResult {
  rubricDomain: string;
  performanceLevel: PerformanceLevel;
  summary: string;
  narrative: string;
  strengths: string[];
  areasForGrowth: string[];
  evidence: AssessmentEvidence[];
}

export interface FocusArea {
  title: string;
  whyItMatters: string;
  evidenceIds: string[];
  suggestedPractice: string;
}

export interface ReferralEvidence {
  evidenceId: string;
  turnId: string;
  turnLabel: string;
  turnIndex: number;
  speaker: "student" | "patient";
  evidenceType: "strength" | "concern" | "missed_opportunity" | "neutral";
  studentExcerpt: string;
  patientContextExcerpt: string;
  whyItMatters: string;
  confidence: "high" | "medium" | "low";
  reviewerConfirmed: boolean;
  domainId: string;
  domainTitle: string;
}

export interface ReferralDomain {
  domainId: string;
  title: string;
  definition: string;
  level: "Strong" | "Appropriate" | "Developing" | "Needs Attention" | "Insufficient Evidence" | "Not Assessed";
  summary: string;
  narrative: string;
  strengths: string[];
  growthAreas: string[];
  strongerApproach: string;
  assessability: "assessed" | "insufficient_evidence" | "not_assessed";
  reviewerStatus: string;
  evidence: ReferralEvidence[];
}

export interface ReferralTimelineEntry {
  turnId: string;
  turnLabel: string;
  turnIndex: number;
  label: string;
  description: string;
  excerpt: string;
  speaker: "student" | "patient";
  evidenceType: string;
}

export interface ReferralAssessment {
  status: "active" | "insufficient_evidence";
  activationReason: string;
  overallLevel: string | null;
  overallSummary: string | null;
  keyStrengths: string[];
  growthOpportunities: string[];
  priorityFocusAreas: string[];
  verificationStatus: string | null;
  domains: ReferralDomain[];
  timeline: ReferralTimelineEntry[];
  keyMoments: ReferralEvidence[];
}

export interface Assessment {
  assessmentId: string;
  assessmentMode: "standard" | "advanced_referral";
  sessionId: string;
  caseId: string;
  status: "PENDING" | "PROCESSING" | "VERIFYING" | "COMPLETE" | "FAILED" | "NEEDS_REVIEW";
  overallLevel: PerformanceLevel | null;
  overallSummary: string | null;
  focusAreas: FocusArea[];
  domains: DomainResult[];
  caseVersion: string;
  rubricVersion: string;
  modelName: string;
  promptVersion: string;
  verificationStatus: string | null;
  createdAt: string;
  completedAt: string | null;
  referral: ReferralAssessment | null;
}

export interface TranscriptMarker {
  evidenceId: string;
  rubricDomain: string;
  evidenceType: EvidenceType;
  label: string;
  severity: string | null;
  confidenceLevel: string;
  reviewerConfirmed: boolean;
  explanation: string;
  whyItMatters: string;
  suggestedAlternative: string;
}

export interface AssessmentTurn {
  turnId: string;
  turnLabel: string;
  sender: "student" | "patient";
  text: string;
  timestamp: string;
  markers: TranscriptMarker[];
}

export interface Rubric {
  rubricId: string;
  domain: string;
  version: string;
  studentFacingDescription: string;
  criteria: string[];
}
