"""Shared constants for the PT AI Patient Simulator backend."""

APP_VERSION = "0.1.0"

# --- PAVING Wheel: the 12 categories in wheel order (clockwise from top), with
# the label colors from the original Harvard PAVING Wheel color system. Values
# are per-case (read from each patient's completed worksheet); labels/colors are
# shared so the digital radar chart is one reusable, data-driven component. ---
PAVING_MAX_VALUE = 25
PAVING_CATEGORIES = (
    {"key": "physical_activity", "label": "Physical Activity", "color": "#5cc8ff"},
    {"key": "attitude", "label": "Attitude", "color": "#f5923e"},
    {"key": "variety", "label": "Variety", "color": "#8ecb3a"},
    {"key": "investigations", "label": "Investigations", "color": "#b79ce8"},
    {"key": "nutrition", "label": "Nutrition", "color": "#ef4f8b"},
    {"key": "goals", "label": "Goals", "color": "#f2c744"},
    {"key": "stress_management", "label": "Stress Management", "color": "#46cfe0"},
    {"key": "time_outs", "label": "Time Outs", "color": "#f5923e"},
    {"key": "energy", "label": "Energy", "color": "#8ecb3a"},
    {"key": "purpose", "label": "Purpose", "color": "#b79ce8"},
    {"key": "sleep", "label": "Sleep", "color": "#ef4f8b"},
    {"key": "social_connections", "label": "Social Connections", "color": "#f2c744"},
)

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
USER_ROLE_SUPER_ADMIN = "super_admin"
USER_ROLES = (USER_ROLE_STUDENT, USER_ROLE_ADMIN, USER_ROLE_SUPER_ADMIN)
# Roles that may reach the admin area (super_admin is a strict superset of admin).
ADMIN_ROLES = (USER_ROLE_ADMIN, USER_ROLE_SUPER_ADMIN)

# --- Runtime configuration: apply modes ---
APPLY_IMMEDIATE = "immediate"          # next provider request uses it
APPLY_NEW_SESSIONS = "new_sessions"    # only interviews started after the change
APPLY_RESTART = "restart_required"     # needs a server restart to take effect

# --- Runtime configuration: server-side validation guards ---
# Approved OpenAI models (only those the strict-JSON Responses flow supports).
OPENAI_MODEL_ALLOWLIST = ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1")
# Approved ElevenLabs models + output formats (must match the current integration).
ELEVENLABS_MODEL_ALLOWLIST = (
    "eleven_turbo_v2_5",
    "eleven_multilingual_v2",
    "eleven_flash_v2_5",
)
ELEVENLABS_FORMAT_ALLOWLIST = ("mp3_44100_128", "mp3_44100_64", "mp3_22050_32")
OPENAI_TIMEOUT_RANGE = (1.0, 120.0)
OPENAI_MAX_TOKENS_RANGE = (16, 4000)
ELEVENLABS_TIMEOUT_RANGE = (1.0, 120.0)
VOICE_STABILITY_RANGE = (0.0, 1.0)
VOICE_SIMILARITY_RANGE = (0.0, 1.0)
VOICE_STYLE_RANGE = (0.0, 1.0)
VOICE_SPEED_RANGE = (0.7, 1.2)

# --- Runtime config audit action types ---
AUDIT_CREDENTIAL_REPLACED = "credential_replaced"
AUDIT_CREDENTIAL_REMOVED = "credential_removed"
AUDIT_CREDENTIAL_TESTED = "credential_tested"
AUDIT_AI_CONFIG_UPDATED = "ai_config_updated"
AUDIT_VOICE_UPDATED = "voice_updated"
AUDIT_VOICE_RESTORED = "voice_restored"
AUDIT_CONFIG_RESTORED = "config_restored"

# --- Audit log action types ---
AUDIT_STUDENT_ARCHIVED = "student_archived"
AUDIT_STUDENT_REACTIVATED = "student_reactivated"
AUDIT_STUDENT_DELETED = "student_deleted"
AUDIT_SESSION_ARCHIVED = "session_archived"
AUDIT_SESSION_DELETED = "session_deleted"
AUDIT_ASSESSMENT_DELETED = "assessment_deleted"
AUDIT_MESSAGE_DELETED = "message_deleted"
# System dashboard actions
AUDIT_VOICE_PREVIEWED = "voice_previewed"
AUDIT_AUDIO_CACHE_CLEARED = "audio_cache_cleared"

# Storage alert threshold (percent used) - configurable real threshold.
STORAGE_WARNING_PERCENT = 80.0

# Fixed, safe voice-preview sample sentences (never free-form text).
VOICE_PREVIEW_SAMPLES = {
    "camden": "Hi, my name is Camden.",
    "carly": "Hi, I'm Carly. Thank you for meeting with me.",
    "sofia": "Hi, I'm Sofia.",
    "jayden": "Hi, I'm Jayden. I'm ready to get started.",
}

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
