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


def _runtime_override_profile(case_id: str, speaker_id: str) -> tuple[VoiceProfile, str] | None:
    """Return (profile, model_id) from an active runtime voice override, if any.

    Opens a short-lived DB session so an admin's dashboard voice edit takes
    effect on the next TTS request. Best-effort: any error falls back to the
    case-file profile."""
    try:
        from sqlalchemy import inspect as _sa_inspect

        from app.database.connection import get_engine, get_session_factory
        from app.models import PatientVoiceSetting

        # has_table() returns False (never raises) when the runtime tables are
        # absent - avoids an exception on the TTS hot path (and the reference
        # cycle a caught traceback would create).
        if not _sa_inspect(get_engine()).has_table("patient_voice_settings"):
            return None
        db = get_session_factory()()
        try:
            row = (
                db.query(PatientVoiceSetting)
                .filter(
                    PatientVoiceSetting.case_id == case_id,
                    PatientVoiceSetting.speaker_id == speaker_id,
                )
                .one_or_none()
            )
            if row is None or not row.is_active or not row.voice_id.strip():
                return None
            prof = VoiceProfile(
                provider="elevenlabs",
                voice_id=row.voice_id,
                model_id=row.model_id,
                stability=row.stability,
                similarity_boost=row.similarity_boost,
                style=row.style,
                # BUGFIX: the admin-saved speed/pace MUST flow into the live
                # runtime VoiceProfile. Previously it was omitted here, so a
                # dashboard speed change persisted and previewed correctly but
                # the next real interview TTS silently used the profile default
                # (0.98) instead of the saved value. The speech-style mapper
                # applies this as the base pace multiplier before ElevenLabs.
                speed=row.speed,
                speaker_boost=row.speaker_boost,
                enabled=True,
            )
            return prof, (row.model_id or get_settings().elevenlabs_default_model)
        finally:
            db.close()
    except Exception:
        return None


def case_file_speaker(case) -> str:
    """Which speaker the case-file ``voice_profile`` represents. For a
    caregiver-primary case (e.g. Camden, whose 4-year-old never speaks) the sole
    spoken voice is the caregiver (his mother), so the case-file voice_profile is
    the mother's voice. Every other case's case-file voice is the patient's."""
    return "caregiver" if getattr(case, "caregiver_primary_only", False) else "patient"


def load_voice_profile(case_id: str, speaker_id: str = "patient") -> ResolvedVoice:
    """Resolve the voice configuration for `case_id` / `speaker_id`.

    Priority: active runtime override -> case-file profile -> unavailable.
    Raises CaseNotFoundError for unknown case IDs (same behavior as the rest of
    the app). Never raises for missing/disabled voice config - it reports
    `available=False` so callers can fall back gracefully.

    Credential/enabled checks use runtime_config_service.elevenlabs_runtime() -
    the SAME resolution ElevenLabsClient itself uses (DB override -> env ->
    default) - never the raw Settings object directly. Before this fix, a key
    stored only via the admin dashboard (DB) but not in the environment made
    this loader report "unavailable" (env-only check) even though the real
    client would have successfully used the DB key, so the frontend silently
    fell back to the robotic browser voice for no real reason.
    """
    case = case_loader.load_case(case_id)  # raises CaseNotFoundError if invalid

    from app.services import runtime_config_service

    rt = runtime_config_service.elevenlabs_runtime()

    override = _runtime_override_profile(case_id, speaker_id)
    if override is not None and rt.enabled and rt.api_key:
        prof, model_id = override
        return ResolvedVoice(available=True, reason="", profile=prof, model_id=model_id)

    # The case-file voice_profile represents exactly ONE speaker: the caregiver
    # for a caregiver-primary case (Camden's mother), otherwise the patient. Any
    # OTHER speaker gets a voice only from a runtime override (handled above);
    # with none we report unavailable so the frontend falls back to a browser
    # voice rather than borrowing the wrong speaker's ElevenLabs voice. This is
    # what guarantees Camden's child ("patient") voice can never be synthesized.
    cf_speaker = case_file_speaker(case)
    if speaker_id not in ("", cf_speaker):
        return ResolvedVoice(
            available=False,
            reason="no_runtime_voice_for_speaker",
            profile=VoiceProfile(),
            model_id=rt.model,
        )

    profile = case.voice_profile or VoiceProfile()

    reason = ""
    if not rt.enabled:
        reason = "elevenlabs_disabled"
    elif not rt.api_key:
        reason = "missing_api_key"
    elif profile.provider != "elevenlabs":
        reason = "unsupported_provider"
    elif not profile.enabled:
        reason = "voice_profile_disabled"
    elif not profile.voice_id.strip() or profile.voice_id.startswith("PASTE_"):
        # Placeholder IDs (PASTE_..._VOICE_ID_HERE) count as "not configured".
        reason = "missing_voice_id"

    model_id = profile.model_id.strip() or rt.model
    return ResolvedVoice(
        available=reason == "",
        reason=reason,
        profile=profile,
        model_id=model_id,
    )
