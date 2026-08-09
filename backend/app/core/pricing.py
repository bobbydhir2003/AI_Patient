"""Central AI provider pricing configuration + cost estimation.

Single source of truth for provider unit prices. Costs shown in the admin
dashboard are ESTIMATED usage costs derived from provider-reported usage units
(OpenAI tokens, ElevenLabs characters) multiplied by these rates — they are NOT
retrieved from provider billing/invoices.

Historical accuracy: every recorded usage event stores the unit prices AND the
`pricing_version` used at record time (see AiUsageEvent), so a later price change
never rewrites the cost of past interviews. Only recompute historical costs if an
admin explicitly asks to.

Prices are expressed per single unit (per token / per character) to avoid scaling
mistakes. Update rates here — never sprinkle prices through the codebase or the
frontend.
"""
from __future__ import annotations

from dataclasses import dataclass

# Bump when any rate below changes so events remain reproducible.
PRICING_VERSION = "2025-01"

# --- OpenAI: USD per single token (input / output / cached input) ------------
# Defaults match the dashboard footer: $0.00015 / 1K input, $0.00060 / 1K output.
_OPENAI_DEFAULT = {
    "input_per_token": 0.00015 / 1000,
    "cached_input_per_token": 0.000075 / 1000,
    "output_per_token": 0.00060 / 1000,
}

# Per-model overrides (fall back to _OPENAI_DEFAULT when a model is absent).
_OPENAI_MODELS: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {
        "input_per_token": 0.00015 / 1000,
        "cached_input_per_token": 0.000075 / 1000,
        "output_per_token": 0.00060 / 1000,
    },
    "gpt-4o": {
        "input_per_token": 0.0025 / 1000,
        "cached_input_per_token": 0.00125 / 1000,
        "output_per_token": 0.010 / 1000,
    },
    "gpt-4.1-mini": {
        "input_per_token": 0.00040 / 1000,
        "cached_input_per_token": 0.00010 / 1000,
        "output_per_token": 0.0016 / 1000,
    },
    "gpt-4.1": {
        "input_per_token": 0.0020 / 1000,
        "cached_input_per_token": 0.0005 / 1000,
        "output_per_token": 0.0080 / 1000,
    },
}

# --- ElevenLabs: USD per single generated character --------------------------
_ELEVENLABS_PER_CHAR = 0.00013


@dataclass(frozen=True)
class OpenAiRates:
    input_per_token: float
    cached_input_per_token: float
    output_per_token: float


def openai_rates(model: str | None) -> OpenAiRates:
    cfg = _OPENAI_MODELS.get((model or "").strip(), _OPENAI_DEFAULT)
    return OpenAiRates(
        input_per_token=cfg["input_per_token"],
        cached_input_per_token=cfg["cached_input_per_token"],
        output_per_token=cfg["output_per_token"],
    )


def elevenlabs_rate_per_char() -> float:
    return _ELEVENLABS_PER_CHAR


def estimate_openai_cost(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    model: str | None = None,
) -> tuple[float, OpenAiRates]:
    """Return (cost_usd, rates_used). Cached input tokens are billed at the
    cheaper cached rate and are NOT double-counted in `input_tokens` (callers
    pass the non-cached input separately when the provider reports caching)."""
    r = openai_rates(model)
    cost = (
        max(0, input_tokens) * r.input_per_token
        + max(0, cached_input_tokens) * r.cached_input_per_token
        + max(0, output_tokens) * r.output_per_token
    )
    return round(cost, 8), r


def estimate_elevenlabs_cost(characters: int) -> tuple[float, float]:
    """Return (cost_usd, per_char_rate)."""
    rate = _ELEVENLABS_PER_CHAR
    return round(max(0, characters) * rate, 8), rate


def pricing_snapshot() -> dict:
    """Human-readable pricing summary for the dashboard footer/admin display."""
    return {
        "version": PRICING_VERSION,
        "openai": {
            "default_input_per_1k": round(_OPENAI_DEFAULT["input_per_token"] * 1000, 6),
            "default_output_per_1k": round(_OPENAI_DEFAULT["output_per_token"] * 1000, 6),
            "models": sorted(_OPENAI_MODELS.keys()),
        },
        "elevenlabs": {"per_character": _ELEVENLABS_PER_CHAR},
    }
