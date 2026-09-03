"""Server-side patient configuration for the `prompt_agent` Realtime engine mode.

This is the SINGLE source of truth that maps a trusted, server-validated
`case_id` (carly/camden/sofia/jayden) onto the OpenAI Realtime settings that
voice that patient: a hosted prompt/config ID, model, voice, reasoning effort
and the server_vad turn-detection tuning.

Design constraints (see the approved prompt_agent architecture):
  - ONE shared worker handles all four patients. There are NOT four workers,
    four APIs, or four OpenAI keys - only four config rows resolved here.
  - The browser NEVER chooses a prompt ID. The worker resolves it from the
    server-side `case_id` bound to the interview job, so a model argument or a
    tampered client payload can never redirect to another patient's prompt.
  - Secrets (prompt IDs) are NEVER hardcoded. Each patient's prompt ID comes
    from an environment-backed Settings field (OPENAI_REALTIME_<NAME>_PROMPT_ID)
    and these values are never exposed to the frontend.
  - If a selected patient has no valid prompt mapping, resolution FAILS LOUDLY
    (raises) rather than guessing or silently falling back to another patient.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.config import Settings

# Canonical case_ids (match backend/app/cases/<id>.json and the assessment
# system). The worker only ever passes one of these; anything else fails.
CARLY = "carly"
CAMDEN = "camden"
SOFIA = "sofia"
JAYDEN = "jayden"

# Shared defaults for every patient. Per-patient overrides live in the table
# below; today only Carly is fully tuned, the other three inherit these so the
# resolver is structurally ready the moment their prompt IDs are provided.
_DEFAULT_MODEL = "gpt-realtime-2.1-mini"
_DEFAULT_REASONING_EFFORT = "low"
# server_vad tuning requested for the prompt_agent runtime. OpenAI Realtime owns
# speech/VAD entirely in this mode (no Deepgram/Silero/Smart Turn), so these are
# the only turn-detection knobs.
_DEFAULT_TURN_DETECTION: dict[str, Any] = {
    "threshold": 0.50,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500,
}


class PatientConfigError(RuntimeError):
    """Raised when a case_id has no valid prompt_agent patient configuration."""


# Non-secret, structural per-patient config. The prompt_id is intentionally
# absent here - it is injected from Settings at resolve time (never hardcoded).
# `prompt_id_setting` names the Settings attribute that carries the secret.
PATIENT_CONFIGS: dict[str, dict[str, Any]] = {
    CARLY: {
        "case_id": CARLY,
        "prompt_id_setting": "openai_realtime_carly_prompt_id",
        "model": _DEFAULT_MODEL,
        "voice": "sage",
        "reasoning_effort": _DEFAULT_REASONING_EFFORT,
        "turn_detection": dict(_DEFAULT_TURN_DETECTION),
    },
    CAMDEN: {
        "case_id": CAMDEN,
        "prompt_id_setting": "openai_realtime_camden_prompt_id",
        "model": _DEFAULT_MODEL,
        "voice": "sage",
        "reasoning_effort": _DEFAULT_REASONING_EFFORT,
        "turn_detection": dict(_DEFAULT_TURN_DETECTION),
    },
    SOFIA: {
        "case_id": SOFIA,
        "prompt_id_setting": "openai_realtime_sofia_prompt_id",
        "model": _DEFAULT_MODEL,
        "voice": "sage",
        "reasoning_effort": _DEFAULT_REASONING_EFFORT,
        "turn_detection": dict(_DEFAULT_TURN_DETECTION),
    },
    JAYDEN: {
        "case_id": JAYDEN,
        "prompt_id_setting": "openai_realtime_jayden_prompt_id",
        "model": _DEFAULT_MODEL,
        "voice": "sage",
        "reasoning_effort": _DEFAULT_REASONING_EFFORT,
        "turn_detection": dict(_DEFAULT_TURN_DETECTION),
    },
}


def resolve_patient_config(case_id: str, settings: "Settings") -> dict[str, Any]:
    """Resolve a validated case_id into a fully-populated prompt_agent config.

    Returns a plain dict carrying model/voice/reasoning_effort/turn_detection
    and the RESOLVED `prompt_id` pulled from Settings. Raises PatientConfigError
    if the case is unknown or its prompt ID has not been configured - the worker
    treats that as a hard start failure rather than voicing the wrong patient or
    an unconfigured one.
    """
    canonical = (case_id or "").strip().lower()
    template = PATIENT_CONFIGS.get(canonical)
    if template is None:
        raise PatientConfigError(
            f"no prompt_agent patient configuration for case_id={case_id!r}"
        )
    prompt_id = str(getattr(settings, template["prompt_id_setting"], "") or "").strip()
    if not prompt_id:
        raise PatientConfigError(
            f"missing OpenAI Realtime prompt ID for case_id={canonical!r} "
            f"(set the {template['prompt_id_setting'].upper()} environment variable)"
        )
    resolved = {
        "case_id": template["case_id"],
        "prompt_id": prompt_id,
        "model": template["model"],
        "voice": template["voice"],
        "reasoning_effort": template["reasoning_effort"],
        "turn_detection": dict(template["turn_detection"]),
    }
    return resolved
