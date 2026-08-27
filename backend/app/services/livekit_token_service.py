"""LiveKit room-scoped token minting - Phase 1/2 POC, and Phase A's new
student-safe token minting.

Not imported by any production interview/voice code path (the real
InterviewPage does not call the student-safe function added here yet - see
its docstring). Mirrors the exact security posture of /voice/synthesize's
ElevenLabs key handling: the LiveKit API secret is read from settings (never
from the client) and never leaves this module - only a short-lived signed
JWT is returned.

Phase 2: the minted token now ALSO carries an explicit LiveKit agent-dispatch
entry (RoomConfiguration/RoomAgentDispatch), so the moment the token holder's
browser creates the room, LiveKit automatically invites our registered
persistent worker (see app/livekit_agent/worker.py) - no SSH command, no
copying a room name into a terminal. The dispatch metadata carries ONLY the
two ids the worker needs (session_id, case_id) - both server-derived from the
ALREADY ownership-verified `session` argument, never patient text, never a
secret. agent_name is the fixed, server-controlled settings.livekit_agent_name
constant - never accepted from the client. The worker itself matches jobs by
agent_name only (see worker.py's WorkerOptions), never by room-name pattern,
so the two distinct room prefixes below require zero worker.py changes.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from app.core.config import get_settings
from app.core.exceptions import LiveKitNotConfiguredError
from app.models import InterviewSession, User

# Two distinct prefixes keep POC/admin rooms and real student-interview rooms
# unmistakably separate in LiveKit Cloud's own dashboard/logs, even though
# both are minted by this same module and dispatched to the same worker.
_POC_ROOM_PREFIX = "ptai-poc-"
_STUDENT_ROOM_PREFIX = "ptai-interview-"


def poc_room_name(session_id: str) -> str:
    """Room name is ALWAYS derived server-side from a verified session id -
    never accepted from the client (see api/livekit.py's ownership check
    BEFORE this is ever called)."""
    return f"{_POC_ROOM_PREFIX}{session_id}"


def student_room_name(session_id: str, connection_id: str) -> str:
    """Phase C3: the room name is no longer just the session id - it also
    carries a fresh, server-generated connection_id (a UUID4, never
    client-supplied - see create_student_token) so every INTENTIONAL voice
    start gets a genuinely new LiveKit room, never a reconnect to a room that
    may still be shutting down from a previous Stop/refresh/leave (the
    confirmed root cause of the "stuck waiting for agent" restart bug: LiveKit
    only re-applies RoomAgentDispatch at room CREATION, so joining a
    still-existing room can silently skip a fresh worker dispatch).
    session_id stays visible in the room name (not replaced) purely for log
    correlation - the WORKER never parses this string for session identity;
    it always reads session_id/case_id from the token's dispatch metadata
    (see _mint_token below), so this suffix cannot affect interview identity.
    Same server-derivation contract as poc_room_name otherwise - see
    api/interviews.py's ownership check BEFORE this is ever called."""
    return f"{_STUDENT_ROOM_PREFIX}{session_id}-{connection_id}"


def poc_participant_identity(user: User) -> str:
    """Identity comes from the authenticated account, never client input."""
    return f"user-{user.id}"


def livekit_configured() -> bool:
    s = get_settings()
    return bool(s.livekit_poc_enabled and s.livekit_url and s.livekit_api_key and s.livekit_api_secret)


def student_livekit_enabled() -> bool:
    """Phase A: the student-safe token endpoint additionally requires
    VOICE_ENGINE=livekit - this is what gives the flag real teeth. With the
    default (VOICE_ENGINE=legacy), this is always False regardless of
    LiveKit Cloud credentials, so create_student_token() below can never be
    reached in production today (no caller has been wired to it yet either -
    see the module docstring)."""
    return livekit_configured() and get_settings().voice_engine == "livekit"


@dataclass(frozen=True)
class PocTokenResult:
    token: str
    url: str
    room_name: str
    participant_identity: str
    # Phase C3: only populated for the student-safe path (create_student_token)
    # - the admin POC path's room stays deterministic (per-session, not
    # per-connection), so this is "" there. Never used for interview
    # identity anywhere - purely a client-facing echo of the room-name
    # suffix, for telemetry/log correlation (see student_room_name).
    connection_id: str = ""


def _mint_token(
    *, user: User, session: InterviewSession, room_name: str, connection_id: str = "",
) -> PocTokenResult:
    """Shared token-construction logic for both the admin POC path and the
    student-safe path below - identical grants/dispatch/expiry, differing
    only in which room-name function and availability check the caller used
    to get here. Callers are responsible for their own availability check
    (livekit_configured() vs student_livekit_enabled()) and for verifying
    session ownership BEFORE calling this - this function does not re-check
    either."""
    settings = get_settings()

    # Local import: keeps the livekit-api dependency isolated to this one POC
    # module rather than a top-level import that every backend process pays
    # for, even ones that never touch the POC.
    from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants

    identity = poc_participant_identity(user)

    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    # Explicit dispatch (Phase 2, "recommended for most applications" per
    # LiveKit's own docs - not automatic dispatch, which invites an agent to
    # EVERY room and cannot carry metadata). agent_name is the fixed,
    # server-controlled constant; metadata is the two ids the worker needs,
    # both taken from the ALREADY ownership-verified `session` row - never
    # from the request payload.
    room_config = RoomConfiguration(
        agents=[
            RoomAgentDispatch(
                agent_name=settings.livekit_agent_name,
                metadata=json.dumps({"session_id": session.id, "case_id": session.case_id}),
            )
        ]
    )
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(user.full_name or user.email or identity)
        .with_grants(grants)
        .with_room_config(room_config)
        .with_ttl(timedelta(minutes=settings.livekit_token_ttl_minutes))
        .to_jwt()
    )
    return PocTokenResult(
        token=token, url=settings.livekit_url, room_name=room_name,
        participant_identity=identity, connection_id=connection_id,
    )


def create_poc_token(*, user: User, session: InterviewSession) -> PocTokenResult:
    """Mint a short-lived LiveKit access token scoped to exactly ONE room
    (the caller's own, already-ownership-verified interview session).

    Caller contract (enforced in app/api/livekit.py, not here): the session
    must already be verified as owned by `user` (or `user` is an admin) via
    the SAME `user_can_access_session` check every other session-scoped
    endpoint uses - this function does not re-check ownership, it only mints
    the token for whatever session it is given.
    """
    if not livekit_configured():
        raise LiveKitNotConfiguredError()
    return _mint_token(user=user, session=session, room_name=poc_room_name(session.id))


def create_student_token(*, user: User, session: InterviewSession) -> PocTokenResult:
    """Phase A: student-safe equivalent of create_poc_token, minted for a
    room name distinct from the admin POC's (see student_room_name).

    Phase C3: a fresh connection_id (UUID4) is generated HERE, server-side,
    on EVERY call - the browser never supplies or influences it (mirrors the
    existing "room name is always server-derived" invariant this module's
    docstring already states). This is what gives every INTENTIONAL voice
    start (first start, Stop-then-Start, refresh, leave/return) its own
    brand-new LiveKit room, so it can never reconnect to a room that may
    still be shutting down from a previous voice connection - see
    student_room_name's docstring for the confirmed race this closes.
    LiveKit's OWN reconnection during an active call (RoomEvent.Reconnecting/
    Reconnected) never calls this function again - it reuses the SAME room
    the frontend already joined, entirely inside livekitPocEngine.ts.

    Caller contract (enforced in app/api/interviews.py, not here): the
    session must already be verified as owned by `user` via
    require_session_access - the SAME dependency /interviews/{id}/messages
    uses for paid-OpenAI-call authorization - this function does not
    re-check ownership. Additionally gated on student_livekit_enabled()
    (VOICE_ENGINE=livekit AND LiveKit Cloud configured), not just
    livekit_configured() - see that function's docstring.
    """
    if not student_livekit_enabled():
        raise LiveKitNotConfiguredError()
    connection_id = str(uuid.uuid4())
    return _mint_token(
        user=user, session=session,
        room_name=student_room_name(session.id, connection_id),
        connection_id=connection_id,
    )
