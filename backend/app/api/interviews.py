from fastapi import APIRouter, Depends

from app.dependencies.auth import (
    get_current_user,
    require_session_access,
)
from app.models import InterviewSession, User
from app.core.rate_limit import rate_limit
from app.patient_engine import case_loader
from app.schemas.interview_schema import InterviewConfigOut
from app.schemas.livekit_schema import LiveKitTokenOut
from app.services import livekit_token_service

router = APIRouter(prefix="/interviews", tags=["interviews"])

# Isolated bucket (Phase A) - minting a LiveKit token must never share/exhaust
# a student's budget, mirroring how voice telemetry had its own bucket.
_livekit_student_rate_limit = rate_limit("livekit_student_token", lambda s: s.voice_rate_limit)


@router.get("/config", response_model=InterviewConfigOut, dependencies=[Depends(get_current_user)])
def interview_config() -> InterviewConfigOut:
    """Student-safe interview configuration for the frontend (no keys, no
    internals). The patient conversation now runs exclusively through the
    LiveKit + OpenAI Realtime voice path, so this only advertises the voice
    engine in use."""
    from app.core.config import get_settings

    return InterviewConfigOut(voice_engine=get_settings().voice_engine)


@router.post(
    "/{session_id}/livekit-token",
    response_model=LiveKitTokenOut,
    dependencies=[Depends(_livekit_student_rate_limit)],
)
def issue_student_livekit_token(
    session: InterviewSession = Depends(require_session_access),
    current_user: User = Depends(get_current_user),
) -> LiveKitTokenOut:
    """Student-safe LiveKit token for the caller's OWN active interview
    session - minted by InterviewPage on session start (the interview voice
    flow). Additionally gated on student_livekit_enabled() - see
    livekit_token_service.py.

    Security, mirroring every other session-scoped endpoint in this app:
    - require_session_access: admins may access any session; a student may only
      access a session owned by their own linked Student profile. An unowned or
      nonexistent session id gets the identical 404 (SessionNotFoundError) -
      no existence leak.
    - Case/session consistency: the session's case must still be a real,
      loadable case (case_loader.load_case raises CaseNotFoundError
      otherwise) - a token is never minted for a room the LiveKit agent
      could never actually generate a patient response for.
    - The room name is derived from the verified session id SERVER-SIDE
      (livekit_token_service.student_room_name) - the client never supplies
      or influences it.
    - Phase C3: every call also mints a fresh, server-generated connection_id
      (UUID4) baked into the room name, so every call gets its OWN brand-new
      LiveKit room - making Stop-then-Start, refresh, and leave/return safe.
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
