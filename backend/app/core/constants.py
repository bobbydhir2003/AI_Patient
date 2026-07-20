"""Shared constants for the PT AI Patient Simulator backend."""

STANDARD_CASE_IDS = ("camden", "carly", "sofia", "jayden")
REFERRAL_CASE_IDS = (
    "referral_case_01",
    "referral_case_02",
    "referral_case_03",
    "referral_case_04",
)
CASE_IDS = STANDARD_CASE_IDS + REFERRAL_CASE_IDS

CASE_CATEGORIES = ("standard", "referral")

CASE_SECTIONS = (
    {
        "id": "standard",
        "title": "Standard PT Cases",
        "description": "Practice patient interviewing and PT communication.",
    },
    {
        "id": "referral",
        "title": "Referral & Interprofessional Cases",
        "description": (
            "Practice recognizing concerns that may require consultation, referral, "
            "care coordination, or escalation beyond the current PT encounter."
        ),
    },
)

# Interview topics the classifier can assign to a student question.
TOPICS = (
    "greeting",
    "condition",
    "symptoms_pain",
    "medications",
    "function_mobility",
    "activity_exercise",
    "home_environment",
    "family_social",
    "school_work",
    "sleep",
    "nutrition",
    "emotional_wellbeing",
    "goals_motivation",
    "healthcare_access",
    "exam_findings",
    "wellness_profile",
    "other",
)

DISCLOSURE_OPEN = "open"            # may be shared whenever the topic comes up
DISCLOSURE_PROBE = "probe"          # shared only when the student asks directly
DISCLOSURE_SENSITIVE = "sensitive"  # shared only when asked with empathy / directly

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_COMPLETED = "completed"

ROLE_STUDENT = "student"
ROLE_PATIENT = "patient"

# --- User account roles (authentication / RBAC) ---
USER_ROLE_STUDENT = "student"
USER_ROLE_ADMIN = "admin"
USER_ROLES = (USER_ROLE_STUDENT, USER_ROLE_ADMIN)

# --- Audit log action types ---
AUDIT_STUDENT_ARCHIVED = "student_archived"
AUDIT_STUDENT_REACTIVATED = "student_reactivated"
AUDIT_STUDENT_DELETED = "student_deleted"
AUDIT_SESSION_ARCHIVED = "session_archived"
AUDIT_SESSION_DELETED = "session_deleted"
AUDIT_ASSESSMENT_DELETED = "assessment_deleted"
AUDIT_MESSAGE_DELETED = "message_deleted"

# --- Extra session status used by the admin panel ---
SESSION_STATUS_ARCHIVED = "archived"

RESPONSE_TYPES = (
    "greeting",
    "small_talk",
    "clinical_answer",
    "follow_up_answer",
    "uncertain",
    "out_of_scope",
)

PROMPT_VERSION = "2.1"  # 2.1: spoken-language style + controlled speech metadata

MAX_FACTS_PER_TURN = 5
MAX_HISTORY_TURNS = 12
MAX_PATIENT_RESPONSE_CHARS = 900

# ---------------- Assessment ----------------
RUBRIC_DOMAINS = (
    "oars_communication",
    "history_checklist",
    "safety_screening",
    "empathy_patient_centeredness",
)

PERFORMANCE_LEVELS = (
    "Advanced",
    "Proficient",
    "Developing",
    "Needs Improvement",
    "Insufficient Evidence",
)

EVIDENCE_TYPES = ("strength", "missed_opportunity", "concern")
CONFIDENCE_LEVELS = ("strong", "moderate", "insufficient")
SEVERITY_LEVELS = ("low", "medium", "high")

ASSESSMENT_STATUS_PENDING = "PENDING"
ASSESSMENT_STATUS_PROCESSING = "PROCESSING"
ASSESSMENT_STATUS_VERIFYING = "VERIFYING"
ASSESSMENT_STATUS_COMPLETE = "COMPLETE"
ASSESSMENT_STATUS_FAILED = "FAILED"
ASSESSMENT_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"

RUBRIC_VERSION = "1.0"
ASSESSMENT_PROMPT_VERSION = "1.0"
MIN_STUDENT_TURNS_FOR_ASSESSMENT = 1  # below this the session cannot be assessed at all

# --- Assessment ---
PERFORMANCE_LEVELS = (
    "Advanced",
    "Proficient",
    "Developing",
    "Needs Improvement",
    "Insufficient Evidence",
)
EVIDENCE_TYPES = ("strength", "missed_opportunity", "mistake", "safety_concern", "observation")
CONFIDENCE_LEVELS = ("strong", "moderate", "insufficient")
SEVERITY_LEVELS = ("minor", "moderate", "important")
ASSESSMENT_STATUSES = ("PENDING", "PROCESSING", "VERIFYING", "COMPLETE", "FAILED", "NEEDS_REVIEW")
ASSESSMENT_PROMPT_VERSION = "1.0"
RUBRIC_DOMAINS = (
    "OARS Communication",
    "History Checklist",
    "Red Flags / Safety Screening",
    "Empathy & Patient-Centeredness",
)
MIN_STUDENT_TURNS_FOR_ASSESSMENT = 1

# --- Advanced referral assessment (universal, case-independent) ---
REFERRAL_LEVELS = (
    "Strong",
    "Appropriate",
    "Developing",
    "Needs Attention",
    "Insufficient Evidence",
    "Not Assessed",
)
REFERRAL_OVERALL_LEVELS = REFERRAL_LEVELS + ("Needs Review",)
REFERRAL_DOMAIN_IDS = (
    "concern_recognition",
    "relevant_exploration",
    "professional_scope",
    "care_pathway_reasoning",
    "urgency_safety",
    "patient_centered_communication",
    "coordination_followup",
)
REFERRAL_EVIDENCE_TYPES = ("strength", "concern", "missed_opportunity", "neutral")
REFERRAL_CONFIDENCE = ("high", "medium", "low")
ASSESSABILITY = ("assessable", "insufficient_evidence", "not_assessed")
SECTION_STANDARD = "standard"
SECTION_REFERRAL = "advanced_referral"
