"""Tests for the Phase 1 LiveKit POC: token endpoint (app/api/livekit.py),
token minting (app/services/livekit_token_service.py), and the agent adapter
(app/livekit_agent/patient_adapter.py, worker.py).

Nothing here touches a real LiveKit Cloud connection - the `livekit`/
`livekit-api` packages are used exactly as installed (real JWT signing, real
SDK types), but no network calls are made. The two most important tests in
this file (test_generate_and_persist_turn_uses_interview_slot and
test_synthesize_patient_audio_pcm_uses_tts_slot) prove the POC agent goes
through the SAME Redis-backed concurrency semaphores as the production
/api/interviews and /api/voice paths - it is not a second, ungoverned
provider-calling path.
"""
import asyncio
import contextlib
import json
import sys
import types

import jwt
import pytest

from app.core.concurrency import interview_slot, tts_slot
from app.core.config import get_settings
from app.livekit_agent import patient_adapter
from app.services import livekit_token_service
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, make_admin, register
from tests.test_voice import FakeElevenLabsClient, give_carly_a_placeholder, give_carly_a_voice_id, seed_owned_session

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


# =================================================================
# Agent adapter: proof the SAME production semaphores/pipeline are reused
# =================================================================

def test_generate_and_persist_turn_uses_interview_slot(monkeypatch, engine, db_session):
    """TEST F (critical): the POC agent's OpenAI call goes through the exact
    same distributed interview_slot() every FastAPI worker uses - not a
    second, ungoverned code path."""
    from tests.conftest import FakeOpenAIClient

    calls: list[str] = []
    real_enter = interview_slot.__enter__
    real_exit = interview_slot.__exit__

    def spy_enter(self):
        calls.append("enter")
        return real_enter(self)

    def spy_exit(self, *exc):
        calls.append("exit")
        return real_exit(self, *exc)

    monkeypatch.setattr(interview_slot, "__enter__", spy_enter)
    monkeypatch.setattr(interview_slot, "__exit__", spy_exit)

    fake_openai = FakeOpenAIClient(text="I've had this pain for about a week.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

    _user, session_id = seed_owned_session(db_session, case_id="carly")

    result = patient_adapter.generate_and_persist_turn(
        db_session, session_id=session_id, case_id="carly",
        question="How long have you had this pain?", client_turn_id="turn-1",
    )

    assert calls == ["enter", "exit"]
    assert fake_openai.calls, "OpenAI was never actually invoked"
    assert result.patient_text == "I've had this pain for about a week."
    assert result.replayed is False


def test_generate_and_persist_turn_is_idempotent_on_client_turn_id(monkeypatch, engine, db_session):
    """TEST K: a retried/duplicate client_turn_id (e.g. a reconnect resending
    the same message) replays the saved turn instead of generating (and
    billing) a second time - the SAME idempotency contract production uses."""
    from tests.conftest import FakeOpenAIClient

    fake_openai = FakeOpenAIClient(text="Same answer every time.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

    _user, session_id = seed_owned_session(db_session, case_id="carly")

    first = patient_adapter.generate_and_persist_turn(
        db_session, session_id=session_id, case_id="carly",
        question="Where does it hurt?", client_turn_id="dup-turn",
    )
    second = patient_adapter.generate_and_persist_turn(
        db_session, session_id=session_id, case_id="carly",
        question="Where does it hurt?", client_turn_id="dup-turn",
    )

    assert len(fake_openai.calls) == 1  # NOT called twice
    assert first.replayed is False
    assert second.replayed is True
    assert second.patient_turn_id == first.patient_turn_id
    assert second.patient_text == first.patient_text


def test_generate_and_persist_turn_raises_for_unknown_session(db_session, engine):
    with pytest.raises(patient_adapter.LiveKitPocSessionNotFoundError):
        patient_adapter.generate_and_persist_turn(
            db_session, session_id="does-not-exist", case_id="carly",
            question="Hi", client_turn_id="t1",
        )


def test_synthesize_patient_audio_pcm_uses_tts_slot(monkeypatch, engine):
    """TEST G (critical): the POC agent's ElevenLabs call goes through the
    exact same distributed tts_slot() every FastAPI worker uses."""
    give_carly_a_voice_id(monkeypatch)
    fake_el = FakeElevenLabsClient(chunks=(b"\x01\x02", b"\x03\x04"))
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

    calls: list[str] = []
    real_acquire = tts_slot.acquire
    real_release = tts_slot.release

    def spy_acquire(self):
        calls.append("acquire")
        return real_acquire(self)

    def spy_release(self):
        calls.append("release")
        return real_release(self)

    monkeypatch.setattr(tts_slot, "acquire", spy_acquire)
    monkeypatch.setattr(tts_slot, "release", spy_release)

    pcm = patient_adapter.synthesize_patient_audio_pcm(case_id="carly", text="Hello there.")

    assert calls == ["acquire", "release"]
    assert pcm == b"\x01\x02\x03\x04"


def test_synthesize_patient_audio_pcm_resolves_correct_voice_profile(monkeypatch, engine):
    """TEST I: the SAME voice_profile_loader/speech_style_mapper resolve the
    voice id/model, and the adapter requests raw PCM (not the production MP3
    default) so no new audio-decoding dependency is required."""
    give_carly_a_voice_id(monkeypatch)
    fake_el = FakeElevenLabsClient()
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

    patient_adapter.synthesize_patient_audio_pcm(case_id="carly", text="Hello there.")

    assert len(fake_el.calls) == 1
    call = fake_el.calls[0]
    assert call["voice_id"] == "real-voice-id"
    assert call["output_format"] == "pcm_16000"
    assert call["text"] == "Hello there."


def test_synthesize_patient_audio_pcm_returns_none_when_voice_not_configured(monkeypatch, engine):
    """No silent fallback to browser TTS anywhere in this module - an
    unconfigured voice simply yields no audio, and the caller (worker.py)
    surfaces that as an explicit POC failure."""
    give_carly_a_placeholder(monkeypatch)
    fake_el = FakeElevenLabsClient()
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

    pcm = patient_adapter.synthesize_patient_audio_pcm(case_id="carly", text="Hello there.")

    assert pcm is None
    assert fake_el.calls == []  # never even attempted


def test_synthesize_patient_audio_pcm_returns_none_when_tts_at_capacity(monkeypatch, engine):
    settings = get_settings()
    give_carly_a_voice_id(monkeypatch)
    fake_el = FakeElevenLabsClient()
    monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)
    # Zero capacity + zero wait -> acquire() cannot possibly succeed.
    monkeypatch.setattr(settings, "max_concurrent_tts_requests", 0)
    monkeypatch.setattr(settings, "tts_wait_seconds", 0.0)

    pcm = patient_adapter.synthesize_patient_audio_pcm(case_id="carly", text="Hello there.")

    assert pcm is None
    assert fake_el.calls == []


# =================================================================
# Phase 2 persistent worker (app/livekit_agent/worker.py) - WorkerOptions /
# JobContext based, replaces the Phase 1 --room/--session-id/--case-id CLI
# script.
# =================================================================

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


def test_frame_size_matches_20ms_at_16khz_mono_16bit():
    """640 bytes = 320 samples x 2 bytes/sample = 20ms @ 16kHz mono PCM16 -
    a conventional WebRTC frame duration. Unchanged from Phase 1."""
    from app.livekit_agent.worker import _FRAME_BYTES

    assert _FRAME_BYTES == 640


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
        def __init__(self, sample_rate, num_channels):
            self.sample_rate = sample_rate
            self.num_channels = num_channels

        async def capture_frame(self, frame):
            pass

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


def _seed_session_with_factory(engine, case_id="carly"):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        _user, session_id = seed_owned_session(db, case_id=case_id)
    finally:
        db.close()
    return factory, session_id


async def _run_one_turn(room, session_id, case_id) -> list[str]:
    from app.livekit_agent.worker import PocAgentSession

    shutdown_reasons: list[str] = []
    session = PocAgentSession(
        room=room, session_id=session_id, case_id=case_id,
        on_shutdown=lambda reason: shutdown_reasons.append(reason),
    )
    await session.start()

    class _Packet:
        topic = "student_text"
        data = json.dumps({"text": "How are you feeling today?", "clientTurnId": "t1"}).encode()

    room.emit("data_received", _Packet())
    for _ in range(25):  # let the fire-and-forget turn task run to completion
        await asyncio.sleep(0.02)
    return shutdown_reasons


def test_poc_agent_session_turn_uses_interview_slot(monkeypatch, engine):
    """TEST H5 (critical, Phase 2): the NEW WorkerOptions/JobContext-driven
    PocAgentSession - not just patient_adapter.py in isolation - still routes
    OpenAI generation through the SAME interview_slot() semaphore. Proves the
    Phase 2 rewrite (room/session/case_id now come from ctx.job.metadata
    instead of argparse) did not introduce a second, ungoverned path."""
    from tests.conftest import FakeOpenAIClient

    factory, session_id = _seed_session_with_factory(engine)
    monkeypatch.setattr("app.livekit_agent.worker.get_db_factory", lambda: factory)

    fake_openai = FakeOpenAIClient(text="I've had it for a few days.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
    # No configured voice -> synthesize_patient_audio_pcm returns None cleanly
    # (already covered elsewhere); this test only cares about interview_slot.

    calls: list[str] = []
    real_enter = interview_slot.__enter__
    real_exit = interview_slot.__exit__

    def spy_enter(self):
        calls.append("enter")
        return real_enter(self)

    def spy_exit(self, *exc):
        calls.append("exit")
        return real_exit(self, *exc)

    monkeypatch.setattr(interview_slot, "__enter__", spy_enter)
    monkeypatch.setattr(interview_slot, "__exit__", spy_exit)

    with _fake_rtc_for_worker():
        room = _FakeAgentRoom()
        asyncio.run(_run_one_turn(room, session_id, "carly"))

    assert calls == ["enter", "exit"]
    assert fake_openai.calls, "OpenAI was never actually invoked through PocAgentSession"


def test_poc_agent_session_turn_uses_tts_slot(monkeypatch, engine):
    """TEST H6 (critical, Phase 2): same proof as above for the ElevenLabs
    tts_slot() semaphore, driven through PocAgentSession end-to-end."""
    factory, session_id = _seed_session_with_factory(engine)
    monkeypatch.setattr("app.livekit_agent.worker.get_db_factory", lambda: factory)

    from tests.conftest import FakeOpenAIClient

    fake_openai = FakeOpenAIClient(text="I've had it for a few days.")
    monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

    give_carly_a_voice_id(monkeypatch)
    fake_el = FakeElevenLabsClient(chunks=(b"\x01\x02", b"\x03\x04"))
    monkeypatch.setattr("app.livekit_agent.patient_adapter.get_elevenlabs_client", lambda: fake_el)

    calls: list[str] = []
    real_acquire = tts_slot.acquire
    real_release = tts_slot.release

    def spy_acquire(self):
        calls.append("acquire")
        return real_acquire(self)

    def spy_release(self):
        calls.append("release")
        return real_release(self)

    monkeypatch.setattr(tts_slot, "acquire", spy_acquire)
    monkeypatch.setattr(tts_slot, "release", spy_release)

    with _fake_rtc_for_worker():
        room = _FakeAgentRoom()
        asyncio.run(_run_one_turn(room, session_id, "carly"))

    assert calls == ["acquire", "release"]
    # Filtered to the turn-lifecycle topic only - published_data also now
    # carries agent_ready/turn_ack control messages (topic "agent_control"),
    # which have no "status" key at all (see Phase C's protocol).
    statuses = [
        p[1]["status"] for p in room.local_participant.published_data if p[0] == "patient_turn_status"
    ]
    assert statuses == ["speaking_started", "speaking_ended"]


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
