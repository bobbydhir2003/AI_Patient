"""Phase 1 LiveKit POC endpoints ONLY.

Not part of the production interview/voice path - /api/interviews/* and
/api/voice/* are completely untouched by this module. Admin-gated (not
reachable by a normal student account) because this is an internal
engineering experiment, not a student-facing feature.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.exceptions import SessionNotFoundError
from app.core.rate_limit import rate_limit
from app.database.connection import get_db
from app.dependencies.auth import require_admin, user_can_access_session
from app.models import InterviewSession, User
from app.schemas.livekit_schema import LiveKitTokenOut, LiveKitTokenRequest
from app.services import livekit_token_service

router = APIRouter(prefix="/livekit", tags=["livekit-poc"])

# Isolated rate-limit bucket (same pattern as _voice_telemetry_rate_limit) so
# POC token requests can never share/exhaust a real endpoint's budget.
_livekit_poc_rate_limit = rate_limit("livekit_poc", lambda s: s.voice_rate_limit)


@router.post("/token", response_model=LiveKitTokenOut, dependencies=[Depends(_livekit_poc_rate_limit)])
def issue_poc_token(
    payload: LiveKitTokenRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> LiveKitTokenOut:
    """Mint a room-scoped LiveKit token for the Phase 1 POC page.

    Security, mirroring every other session-scoped endpoint in this app:
    - require_admin: only an admin/test account may reach this at all.
    - The session must ALSO be owned by (or accessible to) current_user via
      the SAME user_can_access_session() check /interviews and /voice use -
      an admin cannot mint a token for an arbitrary session id that isn't
      theirs, and the 404 response is identical whether the session doesn't
      exist or isn't accessible (no existence leak, same as
      require_session_access).
    - The room name is derived from the verified session id SERVER-SIDE
      (livekit_token_service.poc_room_name) - the client never supplies or
      influences the room name.
    - The LiveKit API secret never leaves livekit_token_service; only the
      signed, short-lived token is returned.
    """
    session = db.get(InterviewSession, payload.session_id)
    if session is None or not user_can_access_session(current_user, session):
        raise SessionNotFoundError(payload.session_id)

    result = livekit_token_service.create_poc_token(user=current_user, session=session)
    return LiveKitTokenOut(
        token=result.token,
        url=result.url,
        room_name=result.room_name,
        participant_identity=result.participant_identity,
    )
