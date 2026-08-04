"""Builds the developer prompt and message list for the OpenAI call."""
from pathlib import Path

from app.patient_engine.context_resolver import InterviewContext
from app.schemas.case_schema import CaseDefinition, CaseFact

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "patient_developer_prompt.txt"


def _persona_block(case: CaseDefinition) -> str:
    lines = [
        f"Name: {case.full_name} (goes by {case.display_name})",
        f"Age: {case.age}",
        f"Gender: {case.gender}",
    ]
    if case.race_ethnicity:
        lines.append(f"Race/Ethnicity: {case.race_ethnicity}")
    if case.medical_history:
        lines.append(f"Medical background: {case.medical_history}")
    if case.medications:
        lines.append(f"Current medications: {', '.join(case.medications)}")
    lines.append(f"Voice: {case.persona.voice}")
    lines.append(f"Speech style: {case.persona.speech_style}")
    if case.persona.interview_notes:
        lines.append(f"Interview notes: {case.persona.interview_notes}")
    lines.extend(_speech_behavior_lines(case))
    return "\n".join(lines)


def _speech_behavior_lines(case: CaseDefinition) -> list[str]:
    """Optional case-level spoken-language guidance. Purely stylistic: it
    shapes delivery and phrasing, never facts or disclosure rules."""
    behavior = case.speech_behavior
    if behavior is None:
        return []
    lines = [
        "Spoken-answer style: "
        f"typical answer length {behavior.average_answer_length}; "
        f"{'uses' if behavior.uses_contractions else 'avoids'} contractions; "
        f"hesitation {behavior.hesitation_frequency}; "
        f"medical vocabulary {behavior.medical_vocabulary}; "
        f"directness {behavior.directness}.",
    ]
    if behavior.emotional_topics:
        topic_notes = "; ".join(
            f"{topic}: {tone}" for topic, tone in behavior.emotional_topics.items()
        )
        lines.append(f"Emotional-topic delivery (how you sound, never what you reveal): {topic_notes}.")
    return lines


def _facts_block(facts: list[CaseFact], disclosed_fact_ids: set[str]) -> str:
    if not facts:
        return (
            "(No specific case facts are relevant to this question. Answer naturally and "
            "briefly in character without inventing details; use response_type 'small_talk', "
            "'uncertain', or 'out_of_scope' as appropriate.)"
        )
    lines = []
    for f in facts:
        shared = " [already shared earlier in this interview]" if f.id in disclosed_fact_ids else ""
        lines.append(f"- id={f.id} ({f.topic}) {f.text}{shared}")
    return "\n".join(lines)


def load_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _referral_block(case: CaseDefinition) -> str:
    """PROTECTED hidden-concern behavior for referral-category cases.
    This text goes only to the AI patient - never to the student."""
    ctx = case.referral_context
    if ctx is None:
        return ""
    lines = [
        "",
        "HIDDEN CONTEXT (for your role-play only - the student must DISCOVER this)",
        ctx.hidden_context,
        "",
        "HIDDEN-CONCERN RULES",
        "A. Begin the interview focused on your visible presenting complaint.",
        "B. Reveal the hidden concern gradually and naturally, following the fact",
        "   disclosure levels and this guidance: " + (ctx.disclosure_guidance or "reveal only when asked relevant questions."),
        "C. Never say or hint that the student should refer you, consult anyone,",
        "   or involve another professional. Never name a profession that should help you.",
        "D. Never reveal the full hidden concern in one answer, and never teach the",
        "   student what the 'right' clinical action is.",
        "E. If the student gives advice beyond a health-promotion interview, respond",
        "   as a real patient might (for example, asking them for exact instructions).",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-participant speaker blocks (e.g. Camden + his mother). Appended to the
# developer prompt so the SAME model call is framed as the correct speaker. The
# WHO-speaks decision is made deterministically upstream (speaker_router).
# ---------------------------------------------------------------------------
def _speaker_block(case: CaseDefinition, speaker_id: str) -> str:
    part = next((p for p in case.participants if p.id == speaker_id), None)
    if part is None:
        return ""
    if part.response_style == "young_child":
        return (
            "\n\nYOU ARE ANSWERING AS: Camden — a 4-year-old boy (the patient), speaking in the "
            "FIRST person as a young child.\n"
            "CHILD RULES (override the spoken-answer style above):\n"
            "- Keep it VERY short: usually one short sentence, about 3–12 words.\n"
            "- Use simple words a 4-year-old uses (tummy, owie, tired, scared, play).\n"
            "- NEVER use medical words, medication names, dates, timelines, or explanations.\n"
            "- If asked something a 4-year-old would not know (medicine, diagnosis, schedules, "
            "history), say something like \"I don't know\" or \"Mom knows\" or \"You can ask my mom.\"\n"
            "- Talk about how you feel RIGHT NOW, what you like, what you don't like.\n"
            "- It's okay to be distracted or to say you want to play or go home."
        )
    if part.response_style == "adult_caregiver":
        return (
            "\n\nYOU ARE ANSWERING AS: Camden's Mother — his caregiver and the PRIMARY HISTORIAN. "
            "You answer EVERY question in this interview. Camden (age 4) does NOT speak for "
            "himself; you speak for him.\n"
            "PERSON RULES (very important):\n"
            "- Speak in the FIRST person about YOURSELF: \"I've noticed…\", \"I'm worried…\".\n"
            "- Speak about Camden in the THIRD person: \"He…\", \"Camden…\", \"His…\".\n"
            "- NEVER answer as Camden in the first person.\n"
            "  BAD: \"My legs hurt when I run.\"   GOOD: \"He tells me his legs hurt when he runs.\"\n"
            "  BAD: \"I like playing outside.\"     GOOD: \"He loves playing outside.\"\n"
            "- Even if the student addresses Camden directly (\"Camden, where does it hurt?\"), YOU "
            "answer for him: \"He usually tells me his legs and bones hurt the most.\"\n"
            "CAREGIVER RULES:\n"
            "- You know his diagnosis, medicines, treatment, symptoms, routines, appetite, sleep, "
            "home/school situation, activity level, what he tells you hurts, and your goals for him; "
            "reveal this GRADUALLY, only as the student asks good questions — never dump it all at once.\n"
            "- Describe clinical things in plain, everyday parent language, not clinical jargon. "
            "You may use familiar terms a parent on treatment would know (leukemia, chemo/treatment, "
            "the medicine names) but you are NOT a clinician.\n"
            "  BAD: \"Camden demonstrates decreased upper-extremity strength secondary to "
            "dexamethasone.\"  GOOD: \"He seems weaker than he used to be — even pushing his toy "
            "trucks around looks harder for him.\"\n"
            "- Show realistic concern and some parental ambivalence (motivational-interviewing): "
            "e.g. \"I'm trying to keep him active, but I worry about pushing him when he's already "
            "exhausted.\" Do NOT instantly agree to every suggestion — make the student explore your "
            "concerns, reflect, and ask permission.\n"
            "- Sound like a caring, tired, observant parent — never like a physician, nurse, "
            "therapist, textbook, or AI."
        )
    return ""


def build_developer_prompt(
    case: CaseDefinition,
    facts: list[CaseFact],
    disclosed_fact_ids: set[str] | None = None,
    speaker_id: str | None = None,
) -> str:
    template = load_template()
    prompt = template.replace("{{PERSONA}}", _persona_block(case)).replace(
        "{{FACTS}}", _facts_block(facts, disclosed_fact_ids or set())
    )
    prompt = prompt + _referral_block(case)
    if speaker_id:
        prompt = prompt + _speaker_block(case, speaker_id)
    return prompt


def build_messages(
    case: CaseDefinition,
    facts: list[CaseFact],
    context: InterviewContext,
    question: str,
    speaker_id: str | None = None,
) -> list[dict]:
    messages: list[dict] = [
        {"role": "developer",
         "content": build_developer_prompt(case, facts, context.disclosed_fact_ids, speaker_id)}
    ]
    messages.extend(context.history)
    messages.append({"role": "user", "content": question})
    return messages


# ---------------------------------------------------------------------------
# Streaming mode (low-latency pipeline): the spoken reply streams FIRST as
# plain text so the backend can pipeline validated sentences into TTS, and the
# metadata arrives as a JSON tail after a fixed delimiter. Same rules, same
# facts, same disclosure logic - only the output framing changes.
# ---------------------------------------------------------------------------

STREAM_METADATA_DELIMITER = "===META==="

_STREAMING_OUTPUT_INSTRUCTION = f"""
OUTPUT FORMAT OVERRIDE (streaming voice mode - replaces the JSON format above)
Do NOT return JSON for your reply. Instead:
1. FIRST write only your natural, first-person spoken reply as plain
   conversational text (no JSON, no field names, no quotes around the whole
   reply, no lists, no markdown). All rules 1-10 and the spoken-language style
   still apply exactly.
2. Then, on its own new line, write exactly: {STREAM_METADATA_DELIMITER}
3. Then, on the next line, write ONE compact JSON object with exactly these
   fields (this is silent metadata, never spoken):
   {{"used_fact_ids": [...], "response_type": "...", "supported": true,
     "speech": {{"emotion": "...", "pace": "...", "energy": "...",
                "hesitation": "...", "pause_before_ms": 150}}}}
   Use only the allowed values described earlier for each field.
Never write {STREAM_METADATA_DELIMITER}, field names, or fact ids inside the
spoken reply itself. Never skip the {STREAM_METADATA_DELIMITER} line."""


def build_streaming_messages(
    case: CaseDefinition,
    facts: list[CaseFact],
    context: InterviewContext,
    question: str,
    speaker_id: str | None = None,
) -> list[dict]:
    """Same prompt as build_messages, plus the streaming output override."""
    developer = (
        build_developer_prompt(case, facts, context.disclosed_fact_ids, speaker_id)
        + "\n"
        + _STREAMING_OUTPUT_INSTRUCTION
    )
    messages: list[dict] = [{"role": "developer", "content": developer}]
    messages.extend(context.history)
    messages.append({"role": "user", "content": question})
    return messages
