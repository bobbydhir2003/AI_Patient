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
from app.models import InterviewSession, User
from app.core.rate_limit import rate_limit
from app.patient_engine import case_loader
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.interview_schema import InterviewConfigOut, StudentMessageRequest, TurnResponse
from app.schemas.livekit_schema import LiveKitTokenOut
from app.services import interview_service, interview_stream_service, livekit_token_service

router = APIRouter(prefix="/interviews", tags=["interviews"])

_interview_rate_limit = rate_limit("interview", lambda s: s.interview_rate_limit)
# Isolated bucket (Phase A) - minting a LiveKit token must never share/exhaust
# a student's real interview-message budget, mirroring how voice telemetry
# has its own bucket separate from /voice/synthesize.
_livekit_student_rate_limit = rate_limit("livekit_student_token", lambda s: s.voice_rate_limit)


@router.get("/config", response_model=InterviewConfigOut, dependencies=[Depends(get_current_user)])
def interview_config() -> InterviewConfigOut:
    """Student-safe feature flags for the frontend (no keys, no internals)."""
    settings = get_settings()
    return InterviewConfigOut(
        streaming_enabled=settings.openai_patient_streaming_enabled,
        sentence_pipelining_enabled=settings.patient_sentence_pipelining_enabled,
        voice_engine=settings.voice_engine,
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


@router.post(
    "/{session_id}/livekit-token",
    response_model=LiveKitTokenOut,
    dependencies=[Depends(_livekit_student_rate_limit)],
)
def issue_student_livekit_token(
    session: InterviewSession = Depends(require_session_access),
    current_user: User = Depends(get_current_user),
) -> LiveKitTokenOut:
    """Phase A: student-safe LiveKit token for the caller's OWN active
    interview session - NOT part of the real interview flow yet (no page
    calls this endpoint; VOICE_ENGINE defaults to "legacy" and
    student_livekit_enabled() additionally gates this closed by default -
    see livekit_token_service.py).

    Security, mirroring every other session-scoped endpoint in this app:
    - require_session_access: the SAME dependency /messages above uses for
      paid-OpenAI-call authorization - admins may access any session; a
      student may only access a session owned by their own linked Student
      profile. An unowned or nonexistent session id gets the identical 404
      (SessionNotFoundError) either way - no existence leak.
    - Case/session consistency: the session's case must still be a real,
      loadable case (case_loader.load_case raises CaseNotFoundError
      otherwise) - a token is never minted for a room the LiveKit agent
      could never actually generate a patient response for.
    - The room name is derived from the verified session id SERVER-SIDE
      (livekit_token_service.student_room_name) - the client never supplies
      or influences it, and it uses a DIFFERENT prefix than the admin POC's
      rooms (ptai-interview- vs ptai-poc-).
    - Phase C3: every call also mints a fresh, server-generated connection_id
      (UUID4) baked into the room name, so every call gets its OWN brand-new
      LiveKit room - the frontend never has to decide "is this a restart" or
      manage room names itself; a new room is simply what every token
      request produces. This is what makes Stop-then-Start, refresh, and
      leave/return safe: none of them can ever reconnect to a room that
      might still be shutting down from a previous voice connection.
    - The LiveKit API secret never leaves livekit_token_service; only the
      signed, short-lived token is returned.
    """
    case_loader.load_case(session.case_id)  # raises CaseNotFoundError if stale/invalid
    result = livekit_token_service.create_student_token(user=current_user, session=session)
    return LiveKitTokenOut(
        token=result.token,
        url=result.url,
        room_name=result.room_name,
        participant_identity=result.participant_identity,
        connection_id=result.connection_id,
    )
