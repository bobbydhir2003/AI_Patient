"""Assessment schemas: internal AI-stage outputs and API responses."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import CONFIDENCE_LEVELS, EVIDENCE_TYPES, PERFORMANCE_LEVELS
from app.schemas.base import CamelModel
from app.schemas.referral_assessment_schema import ReferralOut

# ---------------- internal AI-stage models (validated, never exposed raw) ---


class EvidenceItem(BaseModel):
    evidence_id: str
    turn_label: str  # e.g. "turn_08" (student turn the item is anchored to)
    evidence_type: str  # strength | missed_opportunity | mistake | safety_concern | observation
    label: str
    severity: str = ""  # minor | moderate | important | ""
    student_excerpt: str = ""
    patient_excerpt: str = ""
    explanation: str = ""
    why_it_matters: str = ""
    suggested_alternative: str = ""
    confidence_level: str = "moderate"


class DomainEvidence(BaseModel):
    rubric_domain: str
    evidence_items: list[EvidenceItem] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    domains: list[DomainEvidence]


class DomainEvaluation(BaseModel):
    rubric_domain: str
    performance_level: str
    summary: str
    narrative: str = ""
    strengths: list[str] = Field(default_factory=list)
    areas_for_growth: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewDomainVerdict(BaseModel):
    rubric_domain: str
    approved: bool
    issues: list[str] = Field(default_factory=list)
    rejected_evidence_ids: list[str] = Field(default_factory=list)


class FocusArea(BaseModel):
    title: str
    why_it_matters: str
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_practice: str = ""


class ReviewResult(BaseModel):
    verdicts: list[ReviewDomainVerdict]
    overall_level: str
    overall_summary: str
    focus_areas: list[FocusArea] = Field(default_factory=list)


class AssessmentStatusOut(BaseModel):
    session_id: str
    assessment_id: str | None = None
    status: str
    stage: str
    assessment_mode: str | None = None
    error_code: str | None = None


# ---------------- strict JSON schemas for the OpenAI Responses API ----------

_EVIDENCE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence_id": {"type": "string"},
        "turn_label": {"type": "string"},
        "evidence_type": {"type": "string", "enum": list(EVIDENCE_TYPES)},
        "label": {"type": "string"},
        "severity": {"type": "string", "enum": ["minor", "moderate", "important", ""]},
        "student_excerpt": {"type": "string"},
        "patient_excerpt": {"type": "string"},
        "explanation": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "suggested_alternative": {"type": "string"},
        "confidence_level": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
    },
    "required": [
        "evidence_id", "turn_label", "evidence_type", "label", "severity",
        "student_excerpt", "patient_excerpt", "explanation", "why_it_matters",
        "suggested_alternative", "confidence_level",
    ],
    "additionalProperties": False,
}

EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "domains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rubric_domain": {"type": "string"},
                    "evidence_items": {"type": "array", "items": _EVIDENCE_ITEM_SCHEMA},
                },
                "required": ["rubric_domain", "evidence_items"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["domains"],
    "additionalProperties": False,
}

EVALUATION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "rubric_domain": {"type": "string"},
        "performance_level": {"type": "string", "enum": list(PERFORMANCE_LEVELS)},
        "summary": {"type": "string"},
        "narrative": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "areas_for_growth": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "rubric_domain", "performance_level", "summary", "narrative",
        "strengths", "areas_for_growth", "evidence_ids",
    ],
    "additionalProperties": False,
}

REVIEW_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rubric_domain": {"type": "string"},
                    "approved": {"type": "boolean"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "rejected_evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["rubric_domain", "approved", "issues", "rejected_evidence_ids"],
                "additionalProperties": False,
            },
        },
        "overall_level": {"type": "string", "enum": list(PERFORMANCE_LEVELS)},
        "overall_summary": {"type": "string"},
        "focus_areas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "suggested_practice": {"type": "string"},
                },
                "required": ["title", "why_it_matters", "evidence_ids", "suggested_practice"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts", "overall_level", "overall_summary", "focus_areas"],
    "additionalProperties": False,
}

# ---------------- API response models (camelCase) ----------------------------


class EvidenceOut(CamelModel):
    evidence_id: str
    turn_id: str
    turn_label: str
    evidence_type: str
    label: str
    severity: str | None = None
    student_excerpt: str
    patient_excerpt: str
    explanation: str
    why_it_matters: str
    suggested_alternative: str
    confidence_level: str
    reviewer_confirmed: bool


class DomainResultOut(CamelModel):
    rubric_domain: str
    performance_level: str
    summary: str
    narrative: str
    strengths: list[str]
    areas_for_growth: list[str]
    evidence: list[EvidenceOut]


class FocusAreaOut(CamelModel):
    title: str
    why_it_matters: str
    evidence_ids: list[str]
    suggested_practice: str


class AssessmentOut(CamelModel):
    assessment_id: str
    assessment_mode: str = "standard"
    session_id: str
    case_id: str
    status: str
    overall_level: str | None
    overall_summary: str | None
    focus_areas: list[FocusAreaOut]
    domains: list[DomainResultOut]
    case_version: str
    rubric_version: str
    model_name: str
    prompt_version: str
    verification_status: str | None
    created_at: datetime
    completed_at: datetime | None
    referral: ReferralOut | None = None


class TranscriptMarkerOut(CamelModel):
    evidence_id: str
    rubric_domain: str
    evidence_type: str
    label: str
    severity: str | None
    confidence_level: str
    reviewer_confirmed: bool
    explanation: str
    why_it_matters: str
    suggested_alternative: str


class AssessmentTurnOut(CamelModel):
    turn_id: str
    turn_label: str
    sender: str
    text: str
    timestamp: datetime
    markers: list[TranscriptMarkerOut]


class RubricOut(CamelModel):
    rubric_id: str
    domain: str
    version: str
    student_facing_description: str
    criteria: list[str]
