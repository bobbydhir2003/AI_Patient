from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import StreamingDisabledError
from app.database.connection import get_db, get_db_factory
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.interview_schema import InterviewConfigOut, StudentMessageRequest, TurnResponse
from app.services import interview_service, interview_stream_service

router = APIRouter(prefix="/interviews", tags=["interviews"])


@router.get("/config", response_model=InterviewConfigOut)
def interview_config() -> InterviewConfigOut:
    """Student-safe feature flags for the frontend (no keys, no internals)."""
    settings = get_settings()
    return InterviewConfigOut(
        streaming_enabled=settings.openai_patient_streaming_enabled,
        sentence_pipelining_enabled=settings.patient_sentence_pipelining_enabled,
    )


@router.post("/{session_id}/messages", response_model=TurnResponse)
def send_message(
    session_id: str,
    payload: StudentMessageRequest,
    db: Session = Depends(get_db),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> TurnResponse:
    return interview_service.send_student_message(db, session_id, payload, client=client)


@router.post("/{session_id}/messages/stream")
def send_message_stream(
    session_id: str,
    payload: StudentMessageRequest,
    session_factory=Depends(get_db_factory),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> StreamingResponse:
    """Low-latency streamed exchange (SSE). Feature-flagged; the stable
    non-streaming endpoint above remains the default and the fallback.

    Deliberately does NOT hold a request-scoped DB session: the service opens
    short-lived sessions (context snapshot, final commit) around the stream.
    """
    if not get_settings().openai_patient_streaming_enabled:
        raise StreamingDisabledError()
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
