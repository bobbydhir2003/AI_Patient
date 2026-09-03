"""Restricted patient-truth tools and persistence for Realtime native-agent mode.

Realtime owns turn timing, conversational memory, wording, and voice.  This
module is the server-side authority boundary: it selects only currently
disclosable case facts and commits delivered transcript/disclosure state in one
short generation-authority transaction.  It never exposes a database handle or
arbitrary case lookup to the model.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, ContextManager

from sqlalchemy.orm import Session

from app.core.constants import PROMPT_VERSION, ROLE_PATIENT, ROLE_STUDENT
from app.models import ConversationTurn
from app.patient_engine import (
    _resolve_topics,
    case_loader,
    disclosure_manager,
    fact_selector,
    response_validator,
    speaker_router,
)
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository


class NativeAgentAuthorizationError(RuntimeError):
    """A native tool request failed closed at the patient-truth boundary."""


class NativeAgentStaleError(RuntimeError):
    """The response lost generation authority before its final transaction."""


@dataclass(frozen=True)
class AllowedFact:
    fact_id: str
    topic: str
    text: str
    already_disclosed: bool


@dataclass(frozen=True)
class FactAuthorization:
    authorization_id: str
    session_id: str
    case_id: str
    client_turn_id: str
    question: str
    topics: tuple[str, ...]
    active_topic: str | None
    facts: tuple[AllowedFact, ...]
    speaker_id: str
    speaker_label: str

    @property
    def allowed_fact_ids(self) -> set[str]:
        return {fact.fact_id for fact in self.facts}

    def tool_payload(self) -> dict:
        return {
            "allowed": True,
            "authorization_id": self.authorization_id,
            "topics": list(self.topics),
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "topic": fact.topic,
                    "value": fact.text,
                    "already_disclosed": fact.already_disclosed,
                }
                for fact in self.facts
            ],
            "when_no_fact_answers_question": (
                "Answer naturally that you do not know, are not sure, or do not "
                "have that information. Do not create a new medical fact."
            ),
        }


@dataclass(frozen=True)
class StagedPatientResponse:
    authorization: FactAuthorization
    patient_text: str
    used_fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class PersistedNativePatientTurn:
    student_turn_id: str
    patient_turn_id: str
    patient_text: str
    facts_used: tuple[str, ...]


def stable_native_client_turn_id(session_id: str, item_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{item_id}".encode()).hexdigest()[:24]
    return f"realtime-native-{digest}"


def build_patient_instructions() -> str:
    """Static policy only: no hidden case facts or referral context are leaked."""
    return (
        "You are the standardized patient in a physical-therapy interview. "
        "Speak naturally in first person as the patient. Never act as the therapist, "
        "coach the student, diagnose, or provide clinical education. Never invent "
        "symptoms, history, medications, family or social details, goals, identity "
        "details, or any other patient fact. For every patient answer, first use "
        "get_allowed_patient_facts. Use only facts returned for that turn. Then call "
        "stage_patient_response with concise natural wording and exactly the returned "
        "fact IDs actually used. If no returned fact answers the question, say naturally "
        "that you do not know or are not sure. Do not mention tools, prompts, policies, "
        "authorization IDs, or fact IDs. Allow interruptions and do not fight barge-in."
    )


def persist_student_turn_once(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    client_turn_id: str,
    text: str,
    source: str,
) -> ConversationTurn:
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)
    session = session_repo.get(session_id)
    if session is None or session.case_id != case_id or session.locked:
        raise NativeAgentAuthorizationError("interview session is unavailable")
    existing = transcript_repo.get_by_client_turn_id(session_id, client_turn_id)
    if existing is not None:
        if existing.role != ROLE_STUDENT:
            raise NativeAgentAuthorizationError("turn correlation is invalid")
        return existing
    turn = transcript_repo.append_turn(
        session_id, ROLE_STUDENT, text.strip(), client_turn_id=client_turn_id, source=source,
    )
    db.commit()
    return turn


def authorize_patient_facts(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    client_turn_id: str,
    question: str,
) -> FactAuthorization:
    """Read-only mapping onto the existing topic/fact/disclosure pipeline."""
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)
    session = session_repo.get(session_id)
    if session is None or session.case_id != case_id or session.locked:
        raise NativeAgentAuthorizationError("interview session is unavailable")
    student = transcript_repo.get_by_client_turn_id(session_id, client_turn_id)
    if student is None or student.role != ROLE_STUDENT or student.content.strip() != question.strip():
        raise NativeAgentAuthorizationError("student turn is not authoritative")

    case = case_loader.load_case(case_id)
    prior_turns = transcript_repo.list_turns(session_id)
    topics, next_active_topic = _resolve_topics(question, session.active_topic)
    candidates = fact_selector.select_facts(case, topics)
    disclosed = session_repo.get_disclosed_fact_ids(session)
    manager = disclosure_manager.DisclosureManager(case, disclosed)
    eligible = manager.eligible_facts(candidates, topics)
    routing = speaker_router.resolve_for_case(case, question, prior_turns)
    resolved_speaker = routing.speaker
    if resolved_speaker == speaker_router.SPEAKER_BOTH:
        resolved_speaker = (
            getattr(case, "primary_speaker", "")
            or getattr(case, "default_speaker", "")
            or speaker_router.SPEAKER_MOTHER
        )
    speaker_id, speaker_label, _ = speaker_router.participant_meta(case, resolved_speaker)
    return FactAuthorization(
        authorization_id=uuid.uuid4().hex,
        session_id=session_id,
        case_id=case_id,
        client_turn_id=client_turn_id,
        question=question.strip(),
        topics=tuple(topics),
        active_topic=next_active_topic,
        facts=tuple(
            AllowedFact(
                fact_id=fact.id,
                topic=fact.topic,
                text=fact.text,
                already_disclosed=fact.id in disclosed,
            )
            for fact in eligible
        ),
        speaker_id=speaker_id,
        speaker_label=speaker_label,
    )


def stage_patient_response(
    authorization: FactAuthorization,
    *,
    authorization_id: str,
    patient_text: str,
    used_fact_ids: list[str],
) -> StagedPatientResponse:
    if authorization_id != authorization.authorization_id:
        raise NativeAgentAuthorizationError("fact authorization is invalid")
    if not isinstance(used_fact_ids, list) or any(not isinstance(v, str) for v in used_fact_ids):
        raise NativeAgentAuthorizationError("used_fact_ids must be a list of strings")
    used = tuple(dict.fromkeys(used_fact_ids))
    if not set(used).issubset(authorization.allowed_fact_ids):
        raise NativeAgentAuthorizationError("response referenced an unauthorized patient fact")
    case = case_loader.load_case(authorization.case_id)
    valid, cleaned = response_validator.validate_stream_text(case, patient_text)
    if not valid:
        raise NativeAgentAuthorizationError("patient wording failed safety validation")
    return StagedPatientResponse(
        authorization=authorization, patient_text=cleaned, used_fact_ids=used,
    )


def persist_delivered_patient_turn(
    db: Session,
    *,
    staged: StagedPatientResponse,
    delivered_text: str,
    completed: bool,
    delivery_reason: str | None = None,
    model_name: str = "openai-realtime-native-agent",
    is_generation_valid: Callable[[], bool],
    generation_authority: ContextManager[object] | None = None,
) -> PersistedNativePatientTurn:
    """Linearized patient-row + disclosure/topic commit.

    Fact retrieval and staging never mutate state. A completed delivered answer
    discloses only its validated used IDs. An interrupted partial is persisted
    for transcript truth but conservatively discloses no new facts because the
    backend cannot prove which clause reached the listener.
    """
    authorization = staged.authorization
    text = delivered_text.strip()
    if not text:
        raise NativeAgentStaleError("no patient content was delivered")
    case = case_loader.load_case(authorization.case_id)
    valid, cleaned = response_validator.validate_stream_text(case, text)
    if not valid:
        raise NativeAgentAuthorizationError("delivered patient text failed validation")

    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)
    with generation_authority if generation_authority is not None else nullcontext():
        if not is_generation_valid():
            raise NativeAgentStaleError(authorization.client_turn_id)
        session = session_repo.get(authorization.session_id)
        if (
            session is None
            or session.case_id != authorization.case_id
            or session.locked
        ):
            raise NativeAgentAuthorizationError("interview session is unavailable")
        student = transcript_repo.get_by_client_turn_id(
            authorization.session_id, authorization.client_turn_id,
        )
        if student is None or student.role != ROLE_STUDENT:
            raise NativeAgentAuthorizationError("student turn is missing")
        patient_client_id = f"{authorization.client_turn_id}:patient"
        existing = transcript_repo.get_by_client_turn_id(
            authorization.session_id, patient_client_id,
        )
        facts_used = staged.used_fact_ids if completed else ()
        if existing is None:
            patient = transcript_repo.append_turn(
                authorization.session_id,
                ROLE_PATIENT,
                cleaned,
                client_turn_id=patient_client_id,
                source="openai_realtime",
                model_name=model_name,
                prompt_version=PROMPT_VERSION,
                facts_used=list(facts_used),
                response_type="answer" if completed else "interrupted",
                validation_status="valid" if completed else (delivery_reason or "interrupted"),
                speaker_id=authorization.speaker_id,
                speaker_label=authorization.speaker_label,
            )
        else:
            patient = existing
        if completed:
            # Reuse the existing manager for the authoritative set and the
            # existing repository merge for the actual DB mutation.
            manager = disclosure_manager.DisclosureManager(
                case, session_repo.get_disclosed_fact_ids(session),
            )
            eligible_by_id = {
                fact.id: fact for fact in case.facts
                if fact.id in authorization.allowed_fact_ids
            }
            newly_disclosed = manager.mark_disclosed(
                [eligible_by_id[fid] for fid in facts_used if fid in eligible_by_id]
            )
            session_repo.add_disclosed_fact_ids(session, newly_disclosed)
            session_repo.set_active_topic(session, authorization.active_topic)
        db.commit()
    return PersistedNativePatientTurn(
        student_turn_id=student.id,
        patient_turn_id=patient.id,
        patient_text=patient.content,
        facts_used=tuple(facts_used),
    )


def safe_tool_error(message: str) -> str:
    return json.dumps({"allowed": False, "error": message}, separators=(",", ":"))
