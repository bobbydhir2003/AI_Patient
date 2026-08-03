from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import StreamingDisabledError
from app.database.connection import get_db, get_db_factory
from app.dependencies.auth import (
    authorize_session_from_token,
    get_current_user,
    require_session_access,
)
from app.models import InterviewSession
from app.core.rate_limit import rate_limit
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.interview_schema import InterviewConfigOut, StudentMessageRequest, TurnResponse
from app.services import interview_service, interview_stream_service

router = APIRouter(prefix="/interviews", tags=["interviews"])

_interview_rate_limit = rate_limit("interview", lambda s: s.interview_rate_limit)


@router.get("/config", response_model=InterviewConfigOut, dependencies=[Depends(get_current_user)])
def interview_config() -> InterviewConfigOut:
    """Student-safe feature flags for the frontend (no keys, no internals)."""
    settings = get_settings()
    return InterviewConfigOut(
        streaming_enabled=settings.openai_patient_streaming_enabled,
        sentence_pipelining_enabled=settings.patient_sentence_pipelining_enabled,
    )


@router.post(
    "/{session_id}/messages",
    response_model=TurnResponse,
    dependencies=[Depends(_interview_rate_limit)],
)
def send_message(
    payload: StudentMessageRequest,
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> TurnResponse:
    # A1/A2: authenticated + ownership-checked before any paid OpenAI call.
    return interview_service.send_student_message(db, session.id, payload, client=client)


@router.post("/{session_id}/messages/stream", dependencies=[Depends(_interview_rate_limit)])
def send_message_stream(
    session_id: str,
    payload: StudentMessageRequest,
    request: Request,
    session_factory=Depends(get_db_factory),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> StreamingResponse:
    """Low-latency streamed exchange (SSE). Feature-flagged; the stable
    non-streaming endpoint above remains the default and the fallback.

    Deliberately does NOT hold a request-scoped DB session: the service opens
    short-lived sessions (context snapshot, final commit) around the stream.
    A1/A2: authentication + ownership are verified up front with a short-lived
    session (authorize_session_from_token) so an anonymous or cross-user caller
    gets a clean 401/404 BEFORE any OpenAI call or streaming begins.
    """
    if not get_settings().openai_patient_streaming_enabled:
        raise StreamingDisabledError()
    authorize_session_from_token(session_factory, request, session_id)
    return StreamingResponse(
        interview_stream_service.stream_student_message(
            session_factory, session_id, payload, client=client
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Disable proxy buffering (nginx) so sentences are not held back.
            "X-Accel-Buffering": "no",
        },
    )
