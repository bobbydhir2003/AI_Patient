/**
 * Pre/Post experience survey definitions.
 *
 * The `name` of every item is the EXACT REDCap variable name from the data
 * dictionary and must not be renamed — the backend validates against these same
 * names and rejects anything else. These are the authoritative frontend copies
 * of the question text; no AI-generated placeholder questions are used.
 */

export interface LikertOption {
  value: 1 | 2 | 3 | 4 | 5;
  label: string;
}

/** Shared 1–5 REDCap radio coding for every Likert item in both instruments. */
export const LIKERT_OPTIONS: LikertOption[] = [
  { value: 1, label: "Strongly disagree" },
  { value: 2, label: "Disagree" },
  { value: 3, label: "Neither agree nor disagree" },
  { value: 4, label: "Agree" },
  { value: 5, label: "Strongly agree" },
];

export interface LikertQuestion {
  /** Exact REDCap variable name. */
  name: string;
  prompt: string;
}

export interface OpenEndedQuestion {
  /** Exact REDCap variable name. */
  name: string;
  prompt: string;
}

export const PRE_LIKERT: LikertQuestion[] = [
  { name: "pre_conf_begin", prompt: "I feel confident beginning a conversation with a virtual patient." },
  {
    name: "pre_conf_questions",
    prompt:
      "I feel confident asking questions that help me understand a patient’s symptoms, concerns, and goals.",
  },
  {
    name: "pre_conf_unexpected",
    prompt:
      "I feel confident responding appropriately when a patient provides an unexpected or emotionally challenging response.",
  },
  {
    name: "pre_conf_interview",
    prompt: "I feel confident in my preparedness to conduct a patient-centered interview.",
  },
  {
    name: "pre_helpful_draft",
    prompt: "[DRAFT] I think interacting with this tool will be helpful for my communication skills.",
  },
];

export const POST_LIKERT: LikertQuestion[] = [
  { name: "post_conf_begin", prompt: "I feel confident beginning a conversation with a virtual patient." },
  {
    name: "post_conf_questions",
    prompt:
      "I feel confident asking questions that help me understand a patient’s symptoms, concerns, and goals.",
  },
  {
    name: "post_conf_unexpected",
    prompt:
      "I feel confident responding appropriately when a patient provides an unexpected or emotionally challenging response.",
  },
  {
    name: "post_conf_interview",
    prompt: "I feel confident in my preparedness to conduct a patient-centered interview.",
  },
  {
    name: "post_helpful_draft",
    prompt: "[DRAFT] I think interacting with this tool will be helpful for my communication skills.",
  },
  {
    name: "post_realistic",
    prompt:
      "The AI patient’s responses were realistic enough for me to practice patient communication.",
  },
  {
    name: "post_consistent",
    prompt: "The AI patient’s response were consistent to the questions I asked.",
  },
  {
    name: "post_strengths_weaknesses",
    prompt:
      "Practicing with the AI patient helped me recognize strengths and weaknesses in my communication approach.",
  },
  {
    name: "post_safe_mistakes",
    prompt:
      "I felt comfortable making mistakes while practicing with the AI patient because I knew it was a safe learning environment.",
  },
  {
    name: "post_feedback_accurate",
    prompt: "The AI feedback accurately reflected my communication during the patient encounter.",
  },
  {
    name: "post_feedback_actionable",
    prompt:
      "The feedback provided by the AI gave me clear, actionable suggestions for improving my communication skills.",
  },
  {
    name: "post_use_again",
    prompt: "I would use an AI Standardized Patient again to prepare for future clinical encounters.",
  },
];

export const POST_OPEN_ENDED: OpenEndedQuestion[] = [
  {
    name: "post_oe_most_helpful",
    prompt:
      "What aspect of the AI Standardized Patient experience was most helpful for practicing your communication skills?",
  },
  {
    name: "post_oe_unrealistic",
    prompt: "What made the interaction feel unrealistic, difficult, or less useful?",
  },
  {
    name: "post_oe_one_change",
    prompt:
      "What is one change that would make this activity more valuable for future physical therapy students?",
  },
  {
    name: "post_oe_feedback_type",
    prompt: "What type of feedback would you like to receive after interacting with the AI patient?",
  },
  {
    name: "post_oe_feedback_missing",
    prompt:
      "What information or suggestions were missing from the AI feedback that would have helped you improve?",
  },
];

/** Matches backend MAX_OPEN_ENDED_LEN — keeps payloads bounded. */
export const OPEN_ENDED_MAX_LEN = 5000;
