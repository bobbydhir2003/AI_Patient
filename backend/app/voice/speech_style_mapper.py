"""Maps controlled speech-performance labels to safe ElevenLabs settings.

The OpenAI patient engine may attach controlled labels (emotion, pace, energy,
hesitation) to a response. This module is the ONLY place those labels become
numbers, and every number is clamped, so neither the model nor the frontend can
ever push unsafe values to ElevenLabs.
"""
from dataclasses import dataclass

from app.schemas.case_schema import VoiceProfile

# ---- Controlled enums (single source of truth) ----
EMOTIONS = (
    "neutral", "warm", "relieved", "worried", "anxious",
    "frustrated", "guarded", "sad", "tearful", "confused",
)
PACES = ("very_slow", "slow", "normal", "fast")
ENERGIES = ("low", "normal", "high")
HESITATIONS = ("none", "mild", "moderate")

DEFAULT_SPEECH = {
    "emotion": "neutral",
    "pace": "normal",
    "energy": "normal",
    "hesitation": "none",
    "pause_before_ms": 150,
}

# Pace label -> relative speed multiplier applied to the profile's base speed.
PACE_SPEED = {
    "very_slow": 0.84,
    "slow": 0.91,
    "normal": 0.98,
    "fast": 1.06,
}

# Emotion label -> (stability delta, style delta). Small, bounded adjustments:
# less stability = more expressive delivery; more style = more emphasis.
EMOTION_ADJUST = {
    "neutral": (0.0, 0.0),
    "warm": (-0.02, 0.03),
    "relieved": (0.02, 0.02),
    "worried": (-0.06, 0.05),
    "anxious": (-0.10, 0.06),
    "frustrated": (-0.08, 0.08),
    "guarded": (0.06, -0.03),
    "sad": (-0.04, 0.04),
    "tearful": (-0.10, 0.08),
    "confused": (-0.04, 0.03),
}

# Energy label -> (speed delta, stability delta).
ENERGY_ADJUST = {
    "low": (-0.03, 0.04),
    "normal": (0.0, 0.0),
    "high": (0.03, -0.04),
}

# Hesitation label -> stability delta (slightly less steady delivery).
HESITATION_ADJUST = {"none": 0.0, "mild": -0.03, "moderate": -0.06}

# Safe hard limits (final clamp, regardless of profile or labels).
SPEED_RANGE = (0.7, 1.2)
STABILITY_RANGE = (0.15, 0.9)
SIMILARITY_RANGE = (0.3, 1.0)
STYLE_RANGE = (0.0, 0.6)
PAUSE_BEFORE_MS_RANGE = (0, 1500)


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    return max(lo, min(hi, value))


def normalize_speech_labels(raw: dict | None) -> dict:
    """Validate speech metadata against the controlled enums.

    Invalid or missing labels fall back to safe defaults - the request is never
    rejected because of bad speech metadata (the words still matter more than
    the delivery). Returns a complete, normalized dict.
    """
    raw = raw or {}
    pause = raw.get("pause_before_ms", raw.get("pauseBeforeMs", DEFAULT_SPEECH["pause_before_ms"]))
    try:
        pause = int(pause)
    except (TypeError, ValueError):
        pause = DEFAULT_SPEECH["pause_before_ms"]
    return {
        "emotion": raw.get("emotion") if raw.get("emotion") in EMOTIONS else DEFAULT_SPEECH["emotion"],
        "pace": raw.get("pace") if raw.get("pace") in PACES else DEFAULT_SPEECH["pace"],
        "energy": raw.get("energy") if raw.get("energy") in ENERGIES else DEFAULT_SPEECH["energy"],
        "hesitation": raw.get("hesitation") if raw.get("hesitation") in HESITATIONS else DEFAULT_SPEECH["hesitation"],
        "pause_before_ms": int(_clamp(pause, PAUSE_BEFORE_MS_RANGE)),
    }


@dataclass(frozen=True)
class MappedVoiceSettings:
    """Final, clamped settings sent to ElevenLabs (plus the client-side pause)."""

    speed: float
    stability: float
    similarity_boost: float
    style: float
    use_speaker_boost: bool
    pause_before_ms: int

    def to_elevenlabs(self) -> dict:
        return {
            "speed": self.speed,
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }


def map_speech_style(profile: VoiceProfile, speech: dict | None) -> MappedVoiceSettings:
    """Merge the case's default voice profile with per-response speech labels.

    The labels only shift delivery slightly around the profile's baseline; the
    final values are always clamped to the safe ranges above.
    """
    labels = normalize_speech_labels(speech)

    # Pace: relative to the profile's base speed (0.98 == neutral multiplier).
    pace_mult = PACE_SPEED[labels["pace"]] / PACE_SPEED["normal"]
    energy_speed, energy_stab = ENERGY_ADJUST[labels["energy"]]
    emo_stab, emo_style = EMOTION_ADJUST[labels["emotion"]]
    hes_stab = HESITATION_ADJUST[labels["hesitation"]]

    speed = _clamp(profile.speed * pace_mult + energy_speed, SPEED_RANGE)
    stability = _clamp(profile.stability + emo_stab + energy_stab + hes_stab, STABILITY_RANGE)
    similarity = _clamp(profile.similarity_boost, SIMILARITY_RANGE)
    style = _clamp(profile.style + emo_style, STYLE_RANGE)

    return MappedVoiceSettings(
        speed=round(speed, 3),
        stability=round(stability, 3),
        similarity_boost=round(similarity, 3),
        style=round(style, 3),
        use_speaker_boost=profile.speaker_boost,
        pause_before_ms=labels["pause_before_ms"],
    )
