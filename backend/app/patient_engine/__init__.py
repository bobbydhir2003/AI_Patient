"""Patient simulation engine.

Pipeline: classify -> resolve context -> select facts -> disclosure rules ->
build prompt -> OpenAI (structured output, with retry) -> validate -> result.

There is NO canned-dialogue path. If generation fails after retries, the engine
raises PatientResponseUnavailableError and nothing is persisted.
"""
from dataclasses import dataclass, field

from app.core.constants import PROMPT_VERSION
from app.core.exceptions import PatientEngineError
from app.core.logging import get_logger
from app.models import ConversationTurn
from app.patient_engine import (
    case_loader,
    context_resolver,
    disclosure_manager,
    fact_selector,
    fallback_manager,
    prompt_builder,
    response_validator,
    topic_classifier,
)
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.interview_schema import PatientReply

logger = get_logger(__name__)


@dataclass
class EngineResult:
    text: str
    topics: list[str]
    active_topic: str | None
    response_type: str
    model_name: str
    prompt_version: str = PROMPT_VERSION
    used_fact_ids: list[str] = field(default_factory=list)
    newly_disclosed_fact_ids: set[str] = field(default_factory=set)
    validation_status: str = "valid"
    # Normalized speech-performance labels (controlled enums) or None.
    # Delivery only - never affects the text or what facts were disclosed.
    speech: dict | None = None
    # Which participant produced this response (multi-participant cases).
    speaker_id: str | None = None
    # Provider-reported OpenAI usage for THIS request (input_tokens/output_tokens
    # /model), used for per-session cost recording. None when unavailable (e.g.
    # an interrupted stream) — never fabricated.
    usage: dict | None = None


def _resolve_topics(question: str, previous_active_topic: str | None) -> tuple[list[str], str | None]:
    topics = topic_classifier.classify(question)
    # Follow-up questions ("how does that affect you?") inherit the active topic.
    if previous_active_topic and (topics == ["other"] or topic_classifier.is_follow_up(question)):
        if previous_active_topic not in topics:
            topics = [t for t in topics if t != "other"] + [previous_active_topic]
    substantive = [t for t in topics if t not in ("greeting", "other")]
    next_active_topic = substantive[0] if substantive else previous_active_topic
    return topics, next_active_topic


def generate_patient_response(
    case_id: str,
    question: str,
    turns: list[ConversationTurn],
    disclosed_fact_ids: set[str],
    active_topic: str | None = None,
    client: OpenAIPatientClient | None = None,
    speaker_id: str | None = None,
) -> EngineResult:
    """Generate ONE participant's response. `speaker_id` (e.g. 'camden' /
    'mother') frames the prompt as that speaker; single-speaker cases pass None
    and behave exactly as before. Joint ('both') turns call this twice."""
    case = case_loader.load_case(case_id)
    topics, next_active_topic = _resolve_topics(question, active_topic)
    context = context_resolver.resolve_context(case_id, topics, turns, disclosed_fact_ids, active_topic)
    candidates = fact_selector.select_facts(case, topics)
    manager = disclosure_manager.DisclosureManager(case, context.disclosed_fact_ids)
    eligible = manager.eligible_facts(candidates, topics)
    eligible_ids = {f.id for f in eligible}

    client = client or get_openai_client()
    messages = prompt_builder.build_messages(case, eligible, context, question, speaker_id)

    usage_holder: dict = {}

    def attempt() -> PatientReply:
        reply = client.generate(messages, usage_out=usage_holder)
        valid, cleaned = response_validator.validate_response(case, reply.patient_text)
        if not valid:
            raise PatientEngineError("Generated response failed validation (character break or leakage).")
        reply.patient_text = cleaned
        return reply

    reply = fallback_manager.run_with_retry(attempt, what=f"patient response for case '{case_id}'")

    # Camden (a 4-year-old) must stay developmentally appropriate. Shorten or
    # deflect to the mother if the model produced clinical/long text.
    child_validation = "valid"
    if speaker_id == "camden":
        from app.patient_engine.child_response_validator import validate_child_response

        check = validate_child_response(reply.patient_text)
        reply.patient_text = check.text
        if check.changed:
            child_validation = "child_adjusted"

    # Only accept fact ids that were actually eligible this turn.
    used_ids = [fid for fid in reply.used_fact_ids if fid in eligible_ids]
    newly_disclosed = manager.mark_disclosed([f for f in eligible if f.id in used_ids])

    from app.core.config import get_settings
    from app.voice.speech_style_mapper import normalize_speech_labels

    # Validate/normalize speech metadata against the controlled enums. Invalid
    # or missing labels become safe defaults; the medical text is untouched.
    speech = (
        normalize_speech_labels(reply.speech.model_dump()) if reply.speech is not None else None
    )

    return EngineResult(
        text=reply.patient_text,
        topics=topics,
        active_topic=next_active_topic,
        response_type=reply.response_type,
        model_name=get_settings().openai_model,
        used_fact_ids=used_ids,
        newly_disclosed_fact_ids=newly_disclosed,
        validation_status=child_validation,
        speech=speech,
        speaker_id=speaker_id,
        usage=dict(usage_holder) if usage_holder else None,
    )
