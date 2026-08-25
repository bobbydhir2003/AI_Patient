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
    assert set(body.keys()) == {"token", "url", "roomName", "participantIdentity"}
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
# Standalone worker process (app/livekit_agent/worker.py)
# =================================================================

def test_worker_refuses_to_start_without_livekit_credentials(monkeypatch, capsys):
    """The standalone agent process must fail closed (not start, not crash
    into a half-connected state) when LiveKit Cloud credentials aren't set -
    it must never invent/guess credentials."""
    import sys

    from app.livekit_agent import worker

    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_url", "")
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    monkeypatch.setattr(sys, "argv", ["worker.py", "--room", "ptai-poc-x", "--session-id", "x"])

    import asyncio

    with pytest.raises(SystemExit):
        asyncio.run(worker._amain())


def test_build_agent_token_grants_are_scoped_to_one_room(monkeypatch):
    """The agent mints its own token from the SAME server-side credentials
    used for the student token - never a client-supplied value - scoped to
    exactly the room it was told to join."""
    settings = get_settings()
    _enable_livekit(monkeypatch)

    from app.livekit_agent.worker import AGENT_IDENTITY, _build_agent_token

    token = _build_agent_token("ptai-poc-some-session")
    decoded = jwt.decode(
        token, settings.livekit_api_secret, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert decoded["sub"] == AGENT_IDENTITY
    assert decoded["video"]["room"] == "ptai-poc-some-session"
    assert decoded["video"]["roomJoin"] is True


def test_frame_size_matches_20ms_at_16khz_mono_16bit():
    """640 bytes = 320 samples x 2 bytes/sample = 20ms @ 16kHz mono PCM16 -
    a conventional WebRTC frame duration."""
    from app.livekit_agent.worker import _FRAME_BYTES

    assert _FRAME_BYTES == 640


# --------------------------------------------------------------- room-name/service unit tests
def test_poc_room_name_format():
    assert livekit_token_service.poc_room_name("abc123") == "ptai-poc-abc123"


def test_livekit_configured_false_by_default():
    # Test settings never set these (see conftest.py env defaults), so the
    # POC must report itself unavailable rather than silently "working".
    assert livekit_token_service.livekit_configured() is False
