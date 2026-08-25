"""LiveKit room-scoped token minting - Phase 1 POC only.

Not imported by any production interview/voice code path. Mirrors the exact
security posture of /voice/synthesize's ElevenLabs key handling: the LiveKit
API secret is read from settings (never from the client) and never leaves
this module - only a short-lived signed JWT is returned.
"""
from dataclasses import dataclass
from datetime import timedelta

from app.core.config import get_settings
from app.core.exceptions import LiveKitNotConfiguredError
from app.models import InterviewSession, User

# Room-name prefix makes a POC room unmistakably distinct from anything a
# future production room-per-interview scheme might use.
_ROOM_PREFIX = "ptai-poc-"


def poc_room_name(session_id: str) -> str:
    """Room name is ALWAYS derived server-side from a verified session id -
    never accepted from the client (see api/livekit.py's ownership check
    BEFORE this is ever called)."""
    return f"{_ROOM_PREFIX}{session_id}"


def poc_participant_identity(user: User) -> str:
    """Identity comes from the authenticated account, never client input."""
    return f"user-{user.id}"


def livekit_configured() -> bool:
    s = get_settings()
    return bool(s.livekit_poc_enabled and s.livekit_url and s.livekit_api_key and s.livekit_api_secret)


@dataclass(frozen=True)
class PocTokenResult:
    token: str
    url: str
    room_name: str
    participant_identity: str


def create_poc_token(*, user: User, session: InterviewSession) -> PocTokenResult:
    """Mint a short-lived LiveKit access token scoped to exactly ONE room
    (the caller's own, already-ownership-verified interview session).

    Caller contract (enforced in app/api/livekit.py, not here): the session
    must already be verified as owned by `user` (or `user` is an admin) via
    the SAME `user_can_access_session` check every other session-scoped
    endpoint uses - this function does not re-check ownership, it only mints
    the token for whatever session it is given.
    """
    settings = get_settings()
    if not livekit_configured():
        raise LiveKitNotConfiguredError()

    # Local import: keeps the livekit-api dependency isolated to this one POC
    # module rather than a top-level import that every backend process pays
    # for, even ones that never touch the POC.
    from livekit.api import AccessToken, VideoGrants

    room_name = poc_room_name(session.id)
    identity = poc_participant_identity(user)

    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(user.full_name or user.email or identity)
        .with_grants(grants)
        .with_ttl(timedelta(minutes=settings.livekit_token_ttl_minutes))
        .to_jwt()
    )
    return PocTokenResult(token=token, url=settings.livekit_url, room_name=room_name, participant_identity=identity)
