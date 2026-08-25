"""Request/response schemas for the Phase 1 LiveKit POC token endpoint.

Not used by any production interview/voice endpoint. Deliberately narrow: the
response never includes the LiveKit API secret (only the short-lived signed
token + connection URL + the server-derived room name), matching how
/voice/synthesize never returns the ElevenLabs key.
"""
from app.schemas.base import CamelModel


class LiveKitTokenRequest(CamelModel):
    """The POC always operates on an existing, already-authorized interview
    session - never a client-chosen arbitrary room name."""

    session_id: str


class LiveKitTokenOut(CamelModel):
    token: str
    url: str
    room_name: str
    participant_identity: str
