from pydantic import BaseModel, Field

from app.schemas.base import CamelModel


class CaseFact(BaseModel):
    """A single, source-supported fact about the patient."""

    id: str
    topic: str
    disclosure: str  # open | probe | sensitive
    text: str


class CasePersona(BaseModel):
    voice: str
    speech_style: str
    interview_notes: str = ""


class VoiceProfile(BaseModel):
    """Optional per-case ElevenLabs voice configuration.

    INTERNAL ONLY: never exposed through CaseSummary or any public endpoint.
    All numeric values are re-clamped by the speech-style mapper before use, so
    a mistyped case file can never send unsafe values to ElevenLabs."""

    provider: str = "elevenlabs"
    voice_id: str = ""  # empty => ElevenLabs unavailable; frontend falls back
    model_id: str = ""  # empty => use ELEVENLABS_DEFAULT_MODEL
    speed: float = 0.98
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.1
    speaker_boost: bool = True
    default_emotion: str = "neutral"
    pause_style: str = "natural"
    fallback_rate: float = 0.97  # browser speechSynthesis rate when falling back
    enabled: bool = True


class SpeechBehavior(BaseModel):
    """Optional case-level spoken-language guidance for the OpenAI patient.
    Purely stylistic - it never changes facts or disclosure rules."""

    average_answer_length: str = "1-3 sentences"
    uses_contractions: bool = True
    hesitation_frequency: str = "occasional"  # rare | occasional | frequent
    medical_vocabulary: str = "moderate"  # low | moderate | high
    directness: str = "moderate"  # low | moderate | high
    emotional_topics: dict[str, str] = Field(default_factory=dict)


class ReferralInterprofessionalContext(BaseModel):
    """PROTECTED: hidden interprofessional context for referral cases.
    Never exposed through any public endpoint."""

    clinical_issue: str = ""
    scope_considerations: list[str] = Field(default_factory=list)
    safety_considerations: list[str] = Field(default_factory=list)
    reasonable_care_pathways: list[str] = Field(default_factory=list)


class ReferralContext(BaseModel):
    """PROTECTED referral package: context only - no scoring logic,
    no required questions, no question-to-credit mappings."""

    hidden_context: str
    disclosure_guidance: str = ""
    interprofessional_context: ReferralInterprofessionalContext = Field(
        default_factory=ReferralInterprofessionalContext
    )


class CaseDefinition(BaseModel):
    """Full internal case definition loaded from app/cases/*.json."""

    case_id: str
    case_number: int
    case_category: str = "standard"  # standard | referral (controls grouping)
    display_name: str
    full_name: str
    patient_display_name: str = ""  # card title, e.g. "Jordan M."
    age: int
    gender: str
    race_ethnicity: str = ""
    image: str
    setting: str = ""
    difficulty: str = ""
    estimated_minutes: int | None = None
    referral_context: ReferralContext | None = None  # PROTECTED, referral only
    voice_profile: VoiceProfile | None = None  # INTERNAL, never student-visible
    speech_behavior: SpeechBehavior | None = None  # INTERNAL, prompt-only
    medications: list[str] = Field(default_factory=list)
    medical_history: str = ""
    short_description: str
    referral_reason: str
    student_visible_info: list[str]
    task: str
    persona: CasePersona
    facts: list[CaseFact]
    paving_wheel: dict[str, int | None] = Field(default_factory=dict)
    source_documents: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class CaseSummary(CamelModel):
    """STUDENT-SAFE case card (matches the frontend PatientCase type).
    Must never carry hidden referral context, protected facts, or references."""

    id: str
    case_category: str = "standard"
    name: str
    age: int
    image: str
    short_description: str
    referral_reason: str
    student_visible_info: list[str]
    task: str
    setting: str = ""
    difficulty: str = ""
    estimated_minutes: int | None = None

    @classmethod
    def from_definition(cls, case: CaseDefinition) -> "CaseSummary":
        return cls(
            id=case.case_id,
            case_category=case.case_category,
            name=case.patient_display_name or case.display_name,
            age=case.age,
            image=case.image,
            short_description=case.short_description,
            referral_reason=case.referral_reason,
            student_visible_info=case.student_visible_info,
            task=case.task,
            setting=case.setting,
            difficulty=case.difficulty,
            estimated_minutes=case.estimated_minutes,
        )


class CaseSectionOut(CamelModel):
    id: str
    title: str
    description: str
    cases: list[CaseSummary]


class CaseCatalogOut(CamelModel):
    sections: list[CaseSectionOut]
