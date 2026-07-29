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


class PavingCategoryOut(CamelModel):
    """One PAVING category for the digital radar chart. `value` is None when the
    source worksheet total was illegible (needs review) - never fabricated."""

    key: str
    label: str
    value: int | None
    max_value: int = 25
    label_color: str


class PavingProfileOut(CamelModel):
    """Student-safe PAVING profile: real per-case values + shared label colors."""

    source_file: str = ""
    source_page: int | None = None
    max_value: int = 25
    categories: list[PavingCategoryOut] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)  # keys with no legible total


class CaseParticipant(BaseModel):
    """A speaker in a multi-participant case (e.g. Camden + his mother)."""

    id: str  # camden | mother
    display_name: str
    role: str = "patient"  # patient | caregiver
    age: int | None = None
    primary: bool = False
    voice_key: str = ""  # speaker_id used by the runtime voice system
    response_style: str = "adult"  # young_child | adult_caregiver | adult


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
    # Multi-participant cases (e.g. a pediatric patient with a caregiver). Empty
    # for single-speaker cases, which behave exactly as before.
    participants: list[CaseParticipant] = Field(default_factory=list)
    default_speaker: str = ""  # speaker id used when routing is ambiguous
    # Structured PAVING results read from the patient's completed worksheet:
    # {"source_file","source_page","values": {category_key: int}}. Drives the
    # digital radar chart; the old scanned image is no longer the visible chart.
    paving_profile: dict = Field(default_factory=dict)
    # --- Optional student-safe presentation fields (Case Introduction screen) ---
    # These carry NO hidden/assessment data. They are surfaced on the student-facing
    # case card only for standard cases (see CaseSummary.from_definition).
    patient_type: str = ""  # e.g. "Pediatric Patient", "Adult Patient"
    race_ethnicity_display: str = ""  # short label, e.g. "Caucasian"; falls back to race_ethnicity
    paving_wheel_image: str = ""  # path to the patient's completed PAVING wheel scan
    caregiver_notice: str = ""  # e.g. Camden is accompanied by his mother
    primary_speaker: str = ""  # e.g. "mother" when a caregiver answers for the patient


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
    # --- Optional presentation fields (empty for referral cases to preserve
    # progressive-disclosure protection; never carry hidden/assessment data) ---
    gender: str = ""
    race_ethnicity: str = ""
    patient_type: str = ""
    medical_history: str = ""
    medications: list[str] = Field(default_factory=list)
    paving_wheel_image: str = ""
    caregiver_notice: str = ""
    primary_speaker: str = ""
    paving_profile: PavingProfileOut | None = None

    @staticmethod
    def _build_paving_profile(case: CaseDefinition) -> PavingProfileOut | None:
        from app.core.constants import PAVING_CATEGORIES, PAVING_MAX_VALUE

        profile = case.paving_profile or {}
        values = profile.get("values") or {}
        if not values:
            return None
        categories: list[PavingCategoryOut] = []
        needs_review: list[str] = []
        for cat in PAVING_CATEGORIES:
            raw = values.get(cat["key"])
            v = None
            if isinstance(raw, (int, float)) and 0 <= raw <= PAVING_MAX_VALUE:
                v = int(raw)
            else:
                needs_review.append(cat["key"])
            categories.append(PavingCategoryOut(
                key=cat["key"], label=cat["label"], value=v,
                max_value=PAVING_MAX_VALUE, label_color=cat["color"],
            ))
        return PavingProfileOut(
            source_file=profile.get("source_file", ""),
            source_page=profile.get("source_page"),
            max_value=PAVING_MAX_VALUE,
            categories=categories,
            needs_review=needs_review,
        )

    @classmethod
    def from_definition(cls, case: CaseDefinition) -> "CaseSummary":
        # Clinical presentation fields are surfaced ONLY for standard cases.
        # Referral cases must stay minimal so their hidden educational objective
        # (and markers like medications) cannot leak through the public catalog.
        is_standard = case.case_category == "standard"
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
            gender=case.gender if is_standard else "",
            race_ethnicity=(
                (case.race_ethnicity_display or case.race_ethnicity) if is_standard else ""
            ),
            patient_type=case.patient_type if is_standard else "",
            medical_history=case.medical_history if is_standard else "",
            medications=case.medications if is_standard else [],
            paving_wheel_image=case.paving_wheel_image if is_standard else "",
            caregiver_notice=case.caregiver_notice if is_standard else "",
            primary_speaker=case.primary_speaker if is_standard else "",
            paving_profile=cls._build_paving_profile(case) if is_standard else None,
        )


class CaseSectionOut(CamelModel):
    id: str
    title: str
    description: str
    cases: list[CaseSummary]


class CaseCatalogOut(CamelModel):
    sections: list[CaseSectionOut]
