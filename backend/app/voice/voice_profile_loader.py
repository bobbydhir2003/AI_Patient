"""Loads and validates the active case's voice profile.

The voice ID always comes from the backend case file - the frontend can never
supply or override a voice ID. Missing profiles/IDs simply mean "ElevenLabs is
not available for this case" and the frontend falls back to browser TTS.
"""
from dataclasses import dataclass

from app.core.config import get_settings
from app.patient_engine import case_loader
from app.schemas.case_schema import VoiceProfile


@dataclass(frozen=True)
class ResolvedVoice:
    """The effective voice configuration for a case."""

    available: bool
    reason: str  # dev-log friendly reason when unavailable ("" when available)
    profile: VoiceProfile
    model_id: str


def load_voice_profile(case_id: str) -> ResolvedVoice:
    """Resolve the voice configuration for `case_id`.

    Raises CaseNotFoundError for unknown case IDs (same behavior as the rest of
    the app). Never raises for missing/disabled voice config - it reports
    `available=False` so callers can fall back gracefully.
    """
    settings = get_settings()
    case = case_loader.load_case(case_id)  # raises CaseNotFoundError if invalid
    profile = case.voice_profile or VoiceProfile()

    reason = ""
    if not settings.elevenlabs_enabled:
        reason = "elevenlabs_disabled"
    elif not settings.elevenlabs_api_key:
        reason = "missing_api_key"
    elif profile.provider != "elevenlabs":
        reason = "unsupported_provider"
    elif not profile.enabled:
        reason = "voice_profile_disabled"
    elif not profile.voice_id.strip() or profile.voice_id.startswith("PASTE_"):
        # Placeholder IDs (PASTE_..._VOICE_ID_HERE) count as "not configured".
        reason = "missing_voice_id"

    model_id = profile.model_id.strip() or settings.elevenlabs_default_model
    return ResolvedVoice(
        available=reason == "",
        reason=reason,
        profile=profile,
        model_id=model_id,
    )
