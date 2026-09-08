"""Tests for the LiveKit token endpoint (app/api/livekit.py), token minting
(app/services/livekit_token_service.py), and worker.py's job/session
lifecycle (fixed identity, per-job isolation, disconnect/shutdown).

Nothing here touches a real LiveKit Cloud connection - the `livekit`/
`livekit-api` packages are used exactly as installed (real JWT signing, real
SDK types), but no network calls are made.
"""
import asyncio
import contextlib
import json
import sys
import types

import jwt
import pytest

from app.core.config import get_settings
from app.services import livekit_token_service
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, make_admin, register

LIVEKIT_URL = "wss://fake-project.livekit.cloud"
LIVEKIT_API_KEY = "test-lk-key"
LIVEKIT_API_SECRET = "test-lk-secret-at-least-32-chars-long"


def _enable_livekit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", True)
    monkeypatch.setattr(settings, "livekit_url", LIVEKIT_URL)
    monkeypatch.setattr(settings, "livekit_api_key", LIVEKIT_API_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", LIVEKIT_API_SECRET)


def _admin_client(engine, monkeypatch, *, email="lkadmin@school.edu"):
    _enable_livekit(monkeypatch)
    test_client = make_client(engine, authenticate=False)
    make_admin(engine, email=email, password="adminpass1")
    token = login_token(test_client, email, "adminpass1")
    test_client.headers.update(auth_header(token))
    return test_client


def _owned_session_id(client) -> str:
    resp = client.post(
        "/api/sessions", json={"studentName": "Admin", "studentId": "1", "caseId": "carly"}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["sessionId"]


# --------------------------------------------------------------- TEST A/B/C: token auth
def test_token_requires_authentication(engine):
    client = make_client(engine, authenticate=False)
    r = client.post("/api/livekit/token", json={"sessionId": "does-not-matter"})
    assert r.status_code == 401


def test_token_forbidden_for_students(engine, monkeypatch):
    _enable_livekit(monkeypatch)
    client = make_client(engine, authenticate=False)
    register(client, email="lkstudent@school.edu", password="studpass1", number="LKS1")
    token = login_token(client, "lkstudent@school.edu", "studpass1")
    client.headers.update(auth_header(token))
    session_id = _owned_session_id(client)

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 403


def test_token_success_for_admins_own_session(engine, monkeypatch):
    client = _admin_client(engine, monkeypatch)
    session_id = _owned_session_id(client)

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == LIVEKIT_URL
    assert body["roomName"] == f"ptai-poc-{session_id}"
    assert body["token"]
    assert isinstance(body["token"], str) and body["token"].count(".") == 2  # JWT shape


# --------------------------------------------------------------- TEST D: server-derived room name
def test_room_name_is_always_server_derived_from_session_id(engine, monkeypatch):
    client = _admin_client(engine, monkeypatch)
    session_id = _owned_session_id(client)

    # The request schema has no room-name field at all; passing one is simply
    # ignored (extra fields are dropped, never trusted) - the response room
    # name is still exactly ptai-poc-<verified session id>.
    r = client.post(
        "/api/livekit/token",
        json={"sessionId": session_id, "roomName": "attacker-chosen-room"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["roomName"] == f"ptai-poc-{session_id}"


# --------------------------------------------------------------- cross-session / nonexistent denial
def test_token_denied_for_nonexistent_session(engine, monkeypatch):
    client = _admin_client(engine, monkeypatch)
    r = client.post("/api/livekit/token", json={"sessionId": "no-such-session"})
    assert r.status_code == 404


def test_token_denied_for_students_own_unrelated_session_is_still_401_without_auth(engine, monkeypatch):
    # A raw unauthenticated cross-account attempt: no token at all -> 401,
    # never a 404/200 that would leak whether the session exists.
    _enable_livekit(monkeypatch)
    client = make_client(engine, authenticate=False)
    admin_client = _admin_client(engine, monkeypatch, email="lkadmin2@school.edu")
    session_id = _owned_session_id(admin_client)

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 401


# --------------------------------------------------------------- TEST E: secret never returned
def test_token_response_never_contains_api_secret(engine, monkeypatch):
    client = _admin_client(engine, monkeypatch)
    session_id = _owned_session_id(client)
    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 200
    body = r.json()
    # Phase C3: connectionId is now part of the shared LiveKitTokenOut shape,
    # but the admin POC room stays deterministic (per-session, not
    # per-connection) - confirmed empty here, unlike the student path.
    assert set(body.keys()) == {"token", "url", "roomName", "participantIdentity", "connectionId"}
    assert body["connectionId"] == ""
    serialized = r.text
    assert LIVEKIT_API_SECRET not in serialized

    # The signed token itself decodes with the secret (proving it's real) but
    # the secret is never a claim inside it.
    decoded = jwt.decode(
        body["token"], LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert LIVEKIT_API_SECRET not in str(decoded)
    assert decoded["video"]["room"] == f"ptai-poc-{session_id}"
    assert decoded["video"]["roomJoin"] is True
    assert decoded["iss"] == LIVEKIT_API_KEY


# --------------------------------------------------------------- Phase 2: explicit dispatch
def test_token_embeds_explicit_agent_dispatch_with_fixed_agent_name(engine, monkeypatch):
    """TEST H1/H2/H3 (Phase 2, critical): the minted token carries a
    RoomConfiguration/RoomAgentDispatch entry - this is what makes the
    persistent worker join automatically, with NO SSH command and NO
    copied room name. agent_name is the FIXED, server-controlled
    settings.livekit_agent_name constant (the token request schema has no
    field the client could use to influence it), and the dispatch metadata
    carries exactly session_id/case_id from the verified session - never a
    client-supplied value, never patient text."""
    client = _admin_client(engine, monkeypatch)
    session_id = _owned_session_id(client)
    settings = get_settings()

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 200, r.text
    decoded = jwt.decode(
        r.json()["token"], LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )

    agents = decoded["roomConfig"]["agents"]
    assert len(agents) == 1
    assert agents[0]["agentName"] == settings.livekit_agent_name == "ptai-patient-agent"
    metadata = json.loads(agents[0]["metadata"])
    assert metadata == {"session_id": session_id, "case_id": "carly"}


def test_token_not_configured_returns_503(engine, monkeypatch):
    # LIVEKIT_POC_ENABLED left False (default) - the endpoint must fail closed,
    # not silently mint a token against blank/placeholder credentials.
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", False)
    client = make_client(engine, authenticate=False)
    make_admin(engine, email="lkadmin3@school.edu", password="adminpass1")
    token = login_token(client, "lkadmin3@school.edu", "adminpass1")
    client.headers.update(auth_header(token))
    session_id = _owned_session_id(client)

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "livekit_not_configured"



def test_worker_refuses_to_start_without_full_livekit_configuration(monkeypatch):
    """The persistent worker must fail closed (never start, never invent
    credentials) unless LIVEKIT_POC_ENABLED AND all three LiveKit Cloud
    credentials are set - same fail-closed contract Phase 1 had, now
    expressed as _build_worker_options() instead of an argparse CLI check."""
    from app.livekit_agent import worker

    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", False)
    monkeypatch.setattr(settings, "livekit_url", "")
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")

    with pytest.raises(SystemExit):
        worker._build_worker_options()


def test_worker_options_use_fixed_agent_name_and_settings_credentials(monkeypatch):
    """The worker registers under the SAME fixed agent_name the token service
    dispatches to (a mismatch would silently mean the worker never receives
    any job), and uses app.core.config.get_settings() as the single source of
    truth for credentials - not the framework's own os.environ fallback,
    which would only work with real exported env vars, not backend/.env."""
    from app.livekit_agent import worker

    settings = get_settings()
    _enable_livekit(monkeypatch)

    options = worker._build_worker_options()

    assert options.agent_name == settings.livekit_agent_name == "ptai-patient-agent"
    assert options.ws_url == LIVEKIT_URL
    assert options.api_key == LIVEKIT_API_KEY
    assert options.api_secret == LIVEKIT_API_SECRET
    assert options.entrypoint_fnc is worker.entrypoint
    assert options.request_fnc is worker._handle_job_request


def test_parse_job_metadata_extracts_session_and_case_id():
    """TEST H3: the exact contract livekit_token_service.py's dispatch
    metadata and worker.py's entrypoint() must agree on."""
    from app.livekit_agent.worker import parse_job_metadata

    assert parse_job_metadata('{"session_id": "abc", "case_id": "carly"}') == ("abc", "carly")


@pytest.mark.parametrize(
    "raw",
    ["not json", "{}", '{"session_id": "abc"}', '{"case_id": "carly"}', '{"session_id": "", "case_id": "carly"}', ""],
)
def test_parse_job_metadata_fails_closed_on_malformed_metadata(raw):
    """Never guess a session/case id - malformed or incomplete metadata must
    return None so the caller shuts the job down instead of proceeding."""
    from app.livekit_agent.worker import parse_job_metadata

    assert parse_job_metadata(raw) is None


async def _handle_job_request_with_fake(identity_capture: dict) -> None:
    from app.livekit_agent.worker import _handle_job_request

    class _FakeJobRequest:
        async def accept(self, *, name="", identity="", metadata="", attributes=None):
            identity_capture["identity"] = identity
            identity_capture["name"] = name

    await _handle_job_request(_FakeJobRequest())


def test_job_request_handler_sets_fixed_agent_identity():
    """The worker always joins under the SAME fixed identity
    (AGENT_PARTICIPANT_IDENTITY) rather than the framework's default
    "agent-<job_id>" - required so the frontend's existing "Agent connected"
    diagnostic (livekitPocEngine.ts, unchanged) keeps working with zero
    frontend changes."""
    from app.livekit_agent.worker import AGENT_PARTICIPANT_IDENTITY

    captured: dict = {}
    asyncio.run(_handle_job_request_with_fake(captured))
    assert captured["identity"] == AGENT_PARTICIPANT_IDENTITY == "patient-agent"


# --------------------------------------------------------------- PocAgentSession integration
# PocAgentSession.start()/_publish_pcm do `import livekit.rtc as rtc` at call
# time. Python's `import a.b as c` binds via getattr(sys.modules['a'], 'b'),
# not a fresh sys.modules['a.b'] lookup, once `a.b` is already an attribute of
# `a` (true here, since livekit.agents already imported the real livekit.rtc)
# - so BOTH sys.modules['livekit.rtc'] AND the `rtc` attribute on the already-
# imported `livekit` package must be swapped, or calls keep resolving to the
# real module (which requires the native FFI library, unavailable in CI).
@contextlib.contextmanager
def _fake_rtc_for_worker():
    import livekit  # noqa: F401 - ensures the real package is already imported

    real_rtc = sys.modules.get("livekit.rtc")
    fake_rtc = types.ModuleType("livekit.rtc")

    class _AudioSource:
        def __init__(self, sample_rate, num_channels, queue_size_ms=None):
            self.sample_rate = sample_rate
            self.num_channels = num_channels
            # prompt_agent's small-playout-buffer barge-in fix passes this;
            # the fake has no real queue to size, so it is accepted and
            # otherwise unused.
            self.queue_size_ms = queue_size_ms
            # Phase D2: observable so interrupt tests can prove clear_queue()
            # was actually called, and captured_frames so a test can assert
            # publication genuinely stopped (not just that the task ended).
            self.clear_queue_calls = 0
            self.captured_frames = 0
            # Optional test hook: an asyncio.Event to await on a specific
            # (1-based) frame number, letting a test suspend _publish_pcm
            # mid-stream (simulating "currently speaking") so it can then
            # send interrupt_patient and observe a REAL cancellation, not
            # just one that happens to race a fast in-memory fake. Never set
            # by production code - assigned directly onto the instance by
            # tests that need it (see test_livekit_phase_d2.py).
            self.block_on_frame: int | None = None
            self.block_event: "asyncio.Event | None" = None

        async def capture_frame(self, frame):
            self.captured_frames += 1
            if self.block_on_frame == self.captured_frames and self.block_event is not None:
                await self.block_event.wait()

        def clear_queue(self):
            self.clear_queue_calls += 1

    class _LocalAudioTrack:
        @staticmethod
        def create_audio_track(name, source):
            return object()

    class _TrackPublishOptions:
        pass

    class _AudioFrame:
        def __init__(self, data, sample_rate, num_channels, samples_per_channel):
            self.data = data

    fake_rtc.AudioSource = _AudioSource
    fake_rtc.LocalAudioTrack = _LocalAudioTrack
    fake_rtc.TrackPublishOptions = _TrackPublishOptions
    fake_rtc.AudioFrame = _AudioFrame

    sys.modules["livekit.rtc"] = fake_rtc
    sys.modules["livekit"].rtc = fake_rtc
    try:
        yield fake_rtc
    finally:
        if real_rtc is not None:
            sys.modules["livekit.rtc"] = real_rtc
            sys.modules["livekit"].rtc = real_rtc


class _FakeLocalParticipant:
    def __init__(self):
        # (topic, decoded_json_body, destination_identities) - the third
        # element defaults to [] to match the real SDK's own "empty means
        # broadcast to everyone" semantics (see Phase C's
        # PocAgentSession._destination_identities).
        self.published_data: list[tuple[str, dict, list]] = []

    async def publish_track(self, track, opts):
        pass

    async def publish_data(self, payload, reliable=True, topic="", destination_identities=None):
        self.published_data.append((topic, json.loads(payload.decode()), list(destination_identities or [])))


class _FakeAgentRoom:
    """Stands in for JobContext.room (a real livekit.rtc.Room once
    connected) - PocAgentSession only ever calls .on()/.local_participant on
    it, both reproduced here."""

    def __init__(self, remote_identities: dict | None = None):
        self._handlers: dict = {}
        self.local_participant = _FakeLocalParticipant()
        self.remote_participants: dict = remote_identities or {}

    def on(self, event, cb=None):
        # PocAgentSession uses `room.on(...)` as a DECORATOR
        # (`@room.on("data_received")`), matching the real rtc.Room API -
        # support both the decorator form and a direct `on(event, cb)` call.
        if cb is not None:
            self._handlers[event] = cb
            return cb

        def _decorator(fn):
            self._handlers[event] = fn
            return fn

        return _decorator

    def emit(self, event, *args):
        handler = self._handlers.get(event)
        if handler:
            handler(*args)


def _enable_realtime_and_fake_session(monkeypatch, session):
    """Bypasses the real OpenAI Realtime handshake so start() proceeds past
    its fail-closed check - these tests are about disconnect/shutdown and
    per-job isolation mechanics, not the Realtime session itself."""
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_realtime_engine_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-x")

    class _FakeRealtimeSession:
        input_sample_rate = 24000
        is_ready = True
        close_reason = None

        async def start(self):
            pass

        async def wait_until_ready(self, timeout):
            return True

        async def cancel_active_response(self):
            pass

        async def aclose(self):
            pass

    async def fake_start_prompt_agent_session(settings, identity, track_sid):
        session._realtime_session = _FakeRealtimeSession()
        return session._realtime_session

    monkeypatch.setattr(session, "_start_prompt_agent_session", fake_start_prompt_agent_session)


def test_participant_disconnect_triggers_idempotent_shutdown(monkeypatch, engine):
    """TEST H7: when the STUDENT participant leaves, the job shuts down
    exactly once - a second disconnect event (or a duplicate signal) must
    never raise or double-fire ctx.shutdown(), and the agent's OWN identity
    disconnecting must never trigger a shutdown at all."""
    from app.livekit_agent.worker import AGENT_PARTICIPANT_IDENTITY, PocAgentSession

    with _fake_rtc_for_worker():
        room = _FakeAgentRoom()
        shutdown_reasons: list[str] = []
        session = PocAgentSession(
            room=room, session_id="sess-x", case_id="carly",
            on_shutdown=lambda reason: shutdown_reasons.append(reason),
        )
        # This test is about disconnect/shutdown mechanics, not readiness
        # verification - bypass the real DB existence check (see Phase C's
        # _verify_session_exists) rather than seeding an unrelated session.
        monkeypatch.setattr(session, "_verify_session_exists", lambda: True)
        _enable_realtime_and_fake_session(monkeypatch, session)
        asyncio.run(session.start())

        class _Student:
            identity = "user-1"

        class _Agent:
            identity = AGENT_PARTICIPANT_IDENTITY

        room.emit("participant_disconnected", _Agent())
        assert shutdown_reasons == [], "the agent's own identity must never trigger shutdown"

        room.emit("participant_disconnected", _Student())
        assert shutdown_reasons == ["student_left"]

        room.emit("participant_disconnected", _Student())
        assert shutdown_reasons == ["student_left"], "a second disconnect must be a no-op, not a double-fire"


def test_two_jobs_do_not_share_state(monkeypatch, engine):
    """TEST H8: every job gets its OWN PocAgentSession instance with its own
    session_id/case_id/room/turn-lock/shutdown-flag - shutting one job down,
    or one job's turn lock being held, must never affect a second, unrelated
    job. No module-level mutable state is used anywhere in worker.py."""
    from app.livekit_agent.worker import PocAgentSession

    with _fake_rtc_for_worker():
        room_a, room_b = _FakeAgentRoom(), _FakeAgentRoom()
        shutdowns_a: list[str] = []
        shutdowns_b: list[str] = []
        session_a = PocAgentSession(
            room=room_a, session_id="session-a", case_id="carly",
            on_shutdown=lambda r: shutdowns_a.append(r),
        )
        session_b = PocAgentSession(
            room=room_b, session_id="session-b", case_id="camden",
            on_shutdown=lambda r: shutdowns_b.append(r),
        )
        # Isolation-only test - bypass the real DB existence check (see
        # Phase C's _verify_session_exists) rather than seeding two unrelated
        # sessions purely to satisfy it.
        monkeypatch.setattr(session_a, "_verify_session_exists", lambda: True)
        monkeypatch.setattr(session_b, "_verify_session_exists", lambda: True)
        _enable_realtime_and_fake_session(monkeypatch, session_a)
        _enable_realtime_and_fake_session(monkeypatch, session_b)
        asyncio.run(session_a.start())
        asyncio.run(session_b.start())

        assert session_a.session_id != session_b.session_id
        assert session_a.case_id != session_b.case_id
        assert session_a._turn_lock is not session_b._turn_lock
        assert session_a._audio_source is not session_b._audio_source

        # Simulate job A's turn lock being held (a turn in flight) and confirm
        # job B is completely unaffected.
        async def _hold_lock_a():
            async with session_a._turn_lock:
                assert not session_b._turn_lock.locked()

        asyncio.run(_hold_lock_a())

        # Ending job A must never touch job B's state.
        class _StudentA:
            identity = "user-a"

        room_a.emit("participant_disconnected", _StudentA())
        assert shutdowns_a == ["student_left"]
        assert shutdowns_b == []


# --------------------------------------------------------------- room-name/service unit tests
def test_poc_room_name_format():
    assert livekit_token_service.poc_room_name("abc123") == "ptai-poc-abc123"


def test_livekit_configured_false_by_default(monkeypatch):
    # TEST H9: explicitly isolated from whatever real credentials might be
    # present in the developer's local backend/.env (e.g. for real-device
    # testing against AWS) - this asserts livekit_configured()'s fail-closed
    # LOGIC, not the ambient environment it happens to run in.
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", False)
    monkeypatch.setattr(settings, "livekit_url", "")
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    assert livekit_token_service.livekit_configured() is False
