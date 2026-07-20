"""Advanced referral assessment schemas: AI-stage outputs and API responses.
Everything here is case-independent - no patient names, case ids, or
profession mappings appear in the matrix."""
from pydantic import BaseModel, Field

from app.core.constants import (
    ASSESSABILITY,
    REFERRAL_CONFIDENCE,
    REFERRAL_EVIDENCE_TYPES,
    REFERRAL_LEVELS,
    REFERRAL_OVERALL_LEVELS,
)
from app.schemas.base import CamelModel

# ---------------- internal AI-stage models ----------------------------------


class ReferralEvidenceItem(BaseModel):
    evidence_id: str
    domain_id: str
    turn_label: str
    student_excerpt: str = ""
    patient_context_excerpt: str = ""
    evidence_type: str  # strength | concern | missed_opportunity | neutral
    why_it_matters: str = ""
    confidence: str = "medium"


class ExcerptItem(BaseModel):
    turn_label: str
    excerpt: str

class DomainEvidenceExtraction(BaseModel):
    domain_id: str
    student_evidence: list[ExcerptItem] = Field(default_factory=list)
    patient_context_evidence: list[ExcerptItem] = Field(default_factory=list)
    missed_opportunity_evidence: list[ExcerptItem] = Field(default_factory=list)
    assessability: str  # assessable | insufficient_evidence | not_assessed
    reason: str = ""

class ReferralExtraction(BaseModel):
    referral_status: str  # active | insufficient_evidence
    activation_reason: str = ""
    activation_evidence_ids: list[str] = Field(default_factory=list)
    domain_evidence: list[DomainEvidenceExtraction] = Field(default_factory=list)


class ReferralDomainEvaluation(BaseModel):
    domain_id: str
    level: str
    summary: str
    narrative: str = ""
    strengths: list[str] = Field(default_factory=list)
    growth_areas: list[str] = Field(default_factory=list)
    student_evidence_ids: list[str] = Field(default_factory=list)
    patient_context_evidence_ids: list[str] = Field(default_factory=list)
    stronger_approach: str = ""
    assessability: str = "assessable"


class ReferralDomainReview(BaseModel):
    domain_id: str
    status: str  # accepted | rejected
    reason: str = ""


class ReferralReview(BaseModel):
    overall_assessability: str  # sufficient | limited | insufficient
    verification_status: str  # verified | rejected | needs_review
    domain_reviews: list[ReferralDomainReview] = Field(default_factory=list)
    overall_level: str
    overall_summary: str
    key_strengths: list[str] = Field(default_factory=list)
    growth_opportunities: list[str] = Field(default_factory=list)
    priority_focus_areas: list[str] = Field(default_factory=list)


# ---------------- strict JSON schemas for the Responses API -----------------

REFERRAL_EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "referral_status": {"type": "string", "enum": ["active", "insufficient_evidence"]},
        "activation_reason": {"type": "string"},
        "activation_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "domain_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain_id": {"type": "string"},
                    "student_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "turn_label": {"type": "string"},
                                "excerpt": {"type": "string"}
                            },
                            "required": ["turn_label", "excerpt"],
                            "additionalProperties": False
                        }
                    },
                    "patient_context_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "turn_label": {"type": "string"},
                                "excerpt": {"type": "string"}
                            },
                            "required": ["turn_label", "excerpt"],
                            "additionalProperties": False
                        }
                    },
                    "missed_opportunity_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "turn_label": {"type": "string"},
                                "excerpt": {"type": "string"}
                            },
                            "required": ["turn_label", "excerpt"],
                            "additionalProperties": False
                        }
                    },
                    "assessability": {"type": "string", "enum": list(ASSESSABILITY)},
                    "reason": {"type": "string"}
                },
                "required": [
                    "domain_id", "student_evidence", "patient_context_evidence",
                    "missed_opportunity_evidence", "assessability", "reason"
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["referral_status", "activation_reason", "activation_evidence_ids", "domain_evidence"],
    "additionalProperties": False,
}

REFERRAL_EVALUATION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "domain_id": {"type": "string"},
        "level": {"type": "string", "enum": list(REFERRAL_LEVELS)},
        "summary": {"type": "string"},
        "narrative": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "growth_areas": {"type": "array", "items": {"type": "string"}},
        "student_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "patient_context_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "stronger_approach": {"type": "string"},
        "assessability": {"type": "string", "enum": list(ASSESSABILITY)},
    },
    "required": [
        "domain_id", "level", "summary", "narrative", "strengths",
        "growth_areas", "student_evidence_ids", "patient_context_evidence_ids", "stronger_approach", "assessability",
    ],
    "additionalProperties": False,
}

REFERRAL_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_assessability": {"type": "string", "enum": ["sufficient", "limited", "insufficient"]},
        "verification_status": {"type": "string", "enum": ["verified", "rejected", "needs_review"]},
        "domain_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["accepted", "rejected"]},
                    "reason": {"type": "string"},
                },
                "required": ["domain_id", "status", "reason"],
                "additionalProperties": False,
            },
        },
        "overall_level": {"type": "string", "enum": list(REFERRAL_OVERALL_LEVELS)},
        "overall_summary": {"type": "string"},
        "key_strengths": {"type": "array", "items": {"type": "string"}},
        "growth_opportunities": {"type": "array", "items": {"type": "string"}},
        "priority_focus_areas": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overall_assessability", "verification_status", "domain_reviews", "overall_level", "overall_summary",
        "key_strengths", "growth_opportunities", "priority_focus_areas",
    ],
    "additionalProperties": False,
}

# ---------------- API response models (camelCase, student-safe) -------------


class ReferralEvidenceOut(CamelModel):
    evidence_id: str
    turn_id: str
    turn_label: str
    turn_index: int
    speaker: str
    evidence_type: str
    student_excerpt: str
    patient_context_excerpt: str
    why_it_matters: str
    confidence: str
    reviewer_confirmed: bool
    domain_id: str
    domain_title: str


class ReferralDomainOut(CamelModel):
    domain_id: str
    title: str
    definition: str
    level: str
    summary: str
    narrative: str
    strengths: list[str]
    growth_areas: list[str]
    stronger_approach: str
    assessability: str
    reviewer_status: str
    evidence: list[ReferralEvidenceOut]


class TimelineEntryOut(CamelModel):
    turn_id: str
    turn_label: str
    turn_index: int
    label: str
    description: str
    excerpt: str
    speaker: str
    evidence_type: str


class ReferralOut(CamelModel):
    status: str  # active | insufficient_evidence
    activation_reason: str
    overall_level: str | None
    overall_summary: str | None
    key_strengths: list[str]
    growth_opportunities: list[str]
    priority_focus_areas: list[str]
    verification_status: str | None
    domains: list[ReferralDomainOut]
    timeline: list[TimelineEntryOut]
    key_moments: list[ReferralEvidenceOut]
