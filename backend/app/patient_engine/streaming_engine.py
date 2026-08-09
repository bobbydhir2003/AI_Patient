"""Streaming patient-response engine (low-latency pipeline).

ONE OpenAI streaming request per patient turn:

    classify -> resolve context -> select facts -> disclosure rules ->
    streaming prompt -> OpenAI text deltas -> sentence detection ->
    per-sentence (cumulative) safety validation -> approved sentences yielded
    immediately -> metadata tail parsed after the text completes ->
    final EngineResult for ONE authoritative transcript commit.

Safety model: a sentence is approved only if the ENTIRE cumulative approved
text including it passes the existing response validator plus the streaming
leak checks (validate_stream_text). Nothing unvalidated is ever yielded, so
nothing unvalidated can reach TTS. Metadata never blocks the first sentence:
speech-delivery defaults are derived from the case profile up front, and a
malformed metadata tail degrades safely (no fact ids marked disclosed, default
response_type) without touching the already-approved spoken text.

Cancellation: the caller closing this generator (client disconnect /
interruption) closes the OpenAI stream via the delta generator's finally.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from app.core.constants import MAX_PATIENT_RESPONSE_CHARS, PROMPT_VERSION, RESPONSE_TYPES
from app.core.exceptions import PatientEngineError
from app.core.logging import get_logger
from app.patient_engine import (
    EngineResult,
    _resolve_topics,
    case_loader,
    context_resolver,
    disclosure_manager,
    fact_selector,
    prompt_builder,
    response_validator,
)
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.patient_engine.prompt_builder import STREAM_METADATA_DELIMITER
from app.patient_engine.sentence_stream import SentenceAccumulator

logger = get_logger(__name__)


class FirstSentenceRejectedError(PatientEngineError):
    """The very first sentence failed validation: nothing was spoken, so the
    caller can fall back to the stable non-streaming path safely."""

    code = "stream_first_sentence_rejected"


@dataclass
class StreamSpeech:
    """Early, safe delivery labels (case defaults) for the first sentence."""

    speech: dict


@dataclass
class StreamSentence:
    index: int
    text: str


@dataclass
class StreamCompleted:
    result: EngineResult
    metadata_ok: bool
    sentence_count: int
    usage: dict = field(default_factory=dict)
    truncated: bool = False  # a later sentence was rejected or the cap was hit


def derive_early_speech(case, topics: list[str]) -> dict:
    """Delivery labels available BEFORE the model's metadata arrives.

    Priority: case voice-profile default emotion, then the case's
    emotional-topic guidance mapped conservatively onto the controlled enums.
    Only controlled labels are produced - never raw numeric voice values.
    """
    from app.voice.speech_style_mapper import DEFAULT_SPEECH, EMOTIONS, normalize_speech_labels

    labels = dict(DEFAULT_SPEECH)
    profile = case.voice_profile
    if profile is not None and getattr(profile, "default_emotion", None) in EMOTIONS:
        labels["emotion"] = profile.default_emotion

    behavior = case.speech_behavior
    if behavior is not None and behavior.emotional_topics:
        for topic in topics:
            tone = behavior.emotional_topics.get(topic)
            if not tone:
                continue
            tone = tone.lower()
            if "slow" in tone:
                labels["pace"] = "slow"
            if "quiet" in tone or "soft" in tone or "low" in tone:
                labels["energy"] = "low"
            if "tear" in tone:
                labels["emotion"] = "tearful"
            elif "sad" in tone:
                labels["emotion"] = "sad"
            elif "anxious" in tone or "nervous" in tone:
                labels["emotion"] = "anxious"
            elif "worried" in tone or "concern" in tone:
                labels["emotion"] = "worried"
            elif "guarded" in tone or "reluctant" in tone:
                labels["emotion"] = "guarded"
            break
    return normalize_speech_labels(labels)


def _parse_metadata_tail(meta_raw: str) -> dict | None:
    """Tolerantly parse the JSON metadata tail; None if unusable."""
    raw = meta_raw.strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stream_patient_response(
    case_id: str,
    question: str,
    turns: list,
    disclosed_fact_ids: set[str],
    active_topic: str | None = None,
    client: OpenAIPatientClient | None = None,
    correlation_id: str = "",
    speaker_id: str | None = None,
):
    """Generator yielding StreamSpeech, StreamSentence..., StreamCompleted.

    Raises PatientEngineError (incl. FirstSentenceRejectedError) if no sentence
    could be approved - in that case nothing was yielded for speech and the
    caller may fall back to the stable non-streaming path.
    """
    settings_debug = _debug_enabled()
    t0 = time.monotonic()

    case = case_loader.load_case(case_id)
    topics, next_active_topic = _resolve_topics(question, active_topic)
    context = context_resolver.resolve_context(
        case_id, topics, turns, disclosed_fact_ids, active_topic
    )
    candidates = fact_selector.select_facts(case, topics)
    manager = disclosure_manager.DisclosureManager(case, context.disclosed_fact_ids)
    eligible = manager.eligible_facts(candidates, topics)
    eligible_ids = {f.id for f in eligible}

    client = client or get_openai_client()
    messages = prompt_builder.build_streaming_messages(case, eligible, context, question, speaker_id)

    # Early delivery labels so the first sentence never waits for metadata.
    yield StreamSpeech(speech=derive_early_speech(case, topics))

    accumulator = SentenceAccumulator()
    approved: list[str] = []
    approved_text = ""
    rejected_tail = False  # a non-first sentence failed validation / cap
    in_meta = False
    meta_raw = ""
    pending_raw = ""
    usage: dict = {}
    first_delta_logged = False
    delim = STREAM_METADATA_DELIMITER

    def _approve(candidate: str) -> str | None:
        """Cumulative safety gate. Returns cleaned cumulative text or None."""
        tentative = f"{approved_text} {candidate}".strip() if approved_text else candidate
        if len(tentative) > MAX_PATIENT_RESPONSE_CHARS:
            return None
        valid, cleaned = response_validator.validate_stream_text(case, tentative)
        if not valid:
            return None
        return cleaned

    deltas = client.stream_text(messages, usage_out=usage)
    try:
        for delta in deltas:
            if settings_debug and not first_delta_logged:
                first_delta_logged = True
                logger.info(
                    "stream_timing mark=openai_first_delta ms=%.0f turn=%s",
                    (time.monotonic() - t0) * 1000, correlation_id,
                )
            if in_meta:
                meta_raw += delta
                continue
            pending_raw += delta
            pos = pending_raw.find(delim)
            if pos != -1:
                text_part = pending_raw[:pos]
                meta_raw = pending_raw[pos + len(delim):]
                pending_raw = ""
                in_meta = True
                new_sentences = accumulator.feed(text_part)
                final_fragment = accumulator.flush()
                if final_fragment:
                    new_sentences.append(final_fragment)
            else:
                # Hold back a delimiter-length tail in case it arrives split
                # across deltas; everything before it is safe to process.
                hold = len(delim) - 1
                if len(pending_raw) <= hold:
                    continue
                text_part = pending_raw[:-hold]
                pending_raw = pending_raw[-hold:]
                new_sentences = accumulator.feed(text_part)

            for sentence in new_sentences:
                if rejected_tail:
                    continue  # stop approving after a rejection/cap; keep reading metadata
                cleaned_cumulative = _approve(sentence)
                if cleaned_cumulative is None:
                    if not approved:
                        raise FirstSentenceRejectedError(
                            "The first streamed sentence failed validation."
                        )
                    rejected_tail = True
                    logger.warning(
                        "stream_sentence_rejected turn=%s index=%d (later sentence "
                        "blocked; approved text kept)",
                        correlation_id, len(approved),
                    )
                    continue
                # Yield exactly the newly approved sentence text (the cleaned
                # cumulative text minus what was already approved).
                emitted = cleaned_cumulative[len(approved_text):].strip()
                approved_text = cleaned_cumulative
                approved.append(emitted)
                if settings_debug and len(approved) == 1:
                    logger.info(
                        "stream_timing mark=first_sentence_approved ms=%.0f turn=%s chars=%d",
                        (time.monotonic() - t0) * 1000, correlation_id, len(emitted),
                    )
                yield StreamSentence(index=len(approved) - 1, text=emitted)

        # Stream ended. If the delimiter never appeared, flush remaining text.
        if not in_meta:
            leftovers = accumulator.feed(pending_raw)
            pending_raw = ""
            tail = accumulator.flush()
            if tail:
                leftovers.append(tail)
            for sentence in leftovers:
                if rejected_tail:
                    continue
                cleaned_cumulative = _approve(sentence)
                if cleaned_cumulative is None:
                    if not approved:
                        raise FirstSentenceRejectedError(
                            "The first streamed sentence failed validation."
                        )
                    rejected_tail = True
                    continue
                emitted = cleaned_cumulative[len(approved_text):].strip()
                approved_text = cleaned_cumulative
                approved.append(emitted)
                yield StreamSentence(index=len(approved) - 1, text=emitted)
    finally:
        deltas.close()

    if not approved_text:
        raise PatientEngineError("The streamed patient response was empty.")

    # ---- Metadata (never blocks speech; degrades safely) ----
    from app.core.config import get_settings
    from app.voice.speech_style_mapper import normalize_speech_labels

    meta = _parse_metadata_tail(meta_raw)
    metadata_ok = meta is not None
    if not metadata_ok:
        logger.warning("stream_metadata_fallback turn=%s (tail missing/unparseable)", correlation_id)

    raw_fact_ids = meta.get("used_fact_ids") if metadata_ok else []
    if not isinstance(raw_fact_ids, list):
        raw_fact_ids = []
    used_ids = [fid for fid in raw_fact_ids if isinstance(fid, str) and fid in eligible_ids]
    newly_disclosed = manager.mark_disclosed([f for f in eligible if f.id in used_ids])

    response_type = meta.get("response_type") if metadata_ok else None
    if response_type not in RESPONSE_TYPES:
        response_type = "clinical_answer"

    speech_raw = meta.get("speech") if metadata_ok else None
    speech = normalize_speech_labels(speech_raw) if isinstance(speech_raw, dict) else None

    # Provider-reported usage from the completed stream (None if the stream was
    # interrupted before completion — never fabricated).
    stream_usage = None
    if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
        stream_usage = {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "model": get_settings().openai_model,
        }

    result = EngineResult(
        text=approved_text,
        topics=topics,
        active_topic=next_active_topic,
        response_type=response_type,
        model_name=get_settings().openai_model,
        prompt_version=PROMPT_VERSION,
        used_fact_ids=used_ids,
        newly_disclosed_fact_ids=newly_disclosed,
        validation_status="valid" if not rejected_tail else "valid_truncated_stream",
        speech=speech,
        usage=stream_usage,
    )

    if settings_debug:
        logger.info(
            "stream_timing mark=generation_complete ms=%.0f turn=%s sentences=%d "
            "chars=%d metadata_ok=%s input_tokens=%s output_tokens=%s openai_requests=1",
            (time.monotonic() - t0) * 1000, correlation_id, len(approved),
            len(approved_text), metadata_ok,
            usage.get("input_tokens"), usage.get("output_tokens"),
        )

    yield StreamCompleted(
        result=result,
        metadata_ok=metadata_ok,
        sentence_count=len(approved),
        usage=usage,
        truncated=rejected_tail,
    )


def _debug_enabled() -> bool:
    from app.core.config import get_settings

    return bool(get_settings().debug)
