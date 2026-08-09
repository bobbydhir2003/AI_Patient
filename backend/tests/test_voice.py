"""Tests for the ElevenLabs patient-voice module and /api/voice endpoints.

The ElevenLabs network boundary is faked at the client dependency; nothing here
performs real network calls or needs a real key.
"""
import pytest

from app.core.config import get_settings
from app.core.exceptions import VoiceSynthesisError
from app.schemas.case_schema import VoiceProfile
from app.voice.audio_cache import AudioCache, make_cache_key
from app.voice.elevenlabs_client import get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import (
    DEFAULT_SPEECH,
    PAUSE_BEFORE_MS_RANGE,
    SPEED_RANGE,
    STABILITY_RANGE,
    STYLE_RANGE,
    map_speech_style,
    normalize_speech_labels,
)
from tests.conftest import make_client


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _no_env_proxies(monkeypatch):
    """httpx honors proxy env vars; CI/sandbox proxies must not affect these
    tests (no real network is used anywhere in this file)."""
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _clear_audio_cache():
    """The audio cache is process-global; isolate it per test."""
    from app.voice.audio_cache import get_audio_cache

    get_audio_cache().clear()
    yield
    get_audio_cache().clear()


class FakeElevenLabsClient:
    """Test double at the ElevenLabs boundary."""

    def __init__(self, chunks=(b"ID3fakemp3", b"audio-bytes"), fail=False, fail_midstream=False):
        self.chunks = list(chunks)
        self.fail = fail
        self.fail_midstream = fail_midstream
        self.calls: list[dict] = []

    def stream_speech(self, *, text, voice_id, model_id, voice_settings, output_format=None):
        self.calls.append(
            {
                "text": text,
                "voice_id": voice_id,
                "model_id": model_id,
                "voice_settings": voice_settings,
                "output_format": output_format,
            }
        )
        if self.fail:  # e.g. timeout or API error -> safe generic error
            raise VoiceSynthesisError()
        yield self.chunks[0]
        if self.fail_midstream:
            raise VoiceSynthesisError()
        yield from self.chunks[1:]


def make_voice_client(engine, elevenlabs=None, monkeypatch=None, api_key="test-key", enabled=True):
    """TestClient with the ElevenLabs boundary faked and settings controlled."""
    test_client = make_client(engine)
    app = test_client.app
    if elevenlabs is not None:
        app.dependency_overrides[get_elevenlabs_client] = lambda: elevenlabs
    if monkeypatch is not None:
        settings = get_settings()
        monkeypatch.setattr(settings, "elevenlabs_api_key", api_key)
        monkeypatch.setattr(settings, "elevenlabs_enabled", enabled)
    return test_client


def configured_profile(**overrides) -> VoiceProfile:
    values = dict(voice_id="real-voice-id", speed=0.94, stability=0.5, similarity_boost=0.78, style=0.1)
    values.update(overrides)
    return VoiceProfile(**values)


def give_carly_a_voice_id(monkeypatch):
    """Force a known 'configured' voice id regardless of what the checked-in
    case file currently contains (real id or placeholder)."""
    from app.patient_engine import case_loader

    case = case_loader.load_case("carly")
    monkeypatch.setattr(case.voice_profile, "voice_id", "real-voice-id")


def give_carly_a_placeholder(monkeypatch):
    """Force the 'not yet configured' placeholder state, independent of the
    checked-in case file."""
    from app.patient_engine import case_loader

    case = case_loader.load_case("carly")
    monkeypatch.setattr(case.voice_profile, "voice_id", "PASTE_CARLY_VOICE_ID_HERE")


def _synth_payload(**overrides) -> dict:
    payload = {
        "caseId": "carly",
        "text": "It has been a lot to process, honestly.",
        "speechStyle": {
            "emotion": "worried",
            "pace": "slow",
            "energy": "low",
            "hesitation": "mild",
            "pauseBeforeMs": 350,
        },
    }
    payload.update(overrides)
    return payload


def _carly_session(client, case_id="carly") -> str:
    """Create an interview session owned by the authenticated default student.
    Voice synthesis (A5) requires a session the caller owns."""
    resp = client.post(
        "/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": case_id}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["sessionId"]


def seed_owned_session(db, case_id="carly"):
    """Direct-DB helper for tests that call the endpoint function directly:
    returns (student_user, session_id) with the session owned by that student."""
    from app.core.security import hash_password
    from app.models import InterviewSession, Student, User

    student = Student(name="Voice Student", student_number="V1", email="v1@school.edu")
    db.add(student)
    db.flush()
    user = User(
        email="v1@school.edu", password_hash=hash_password("x"), full_name="Voice Student",
        role="student", student_id=student.id, is_active=True,
    )
    db.add(user)
    session = InterviewSession(student_id=student.id, case_id=case_id, case_category="standard")
    db.add(session)
    db.commit()
    return user, session.id


# ------------------------------------------------- speech-style mapper
def test_invalid_speech_labels_fall_back_to_defaults():
    normalized = normalize_speech_labels(
        {"emotion": "melodramatic", "pace": "warp", "energy": "over9000", "hesitation": "yes", "pause_before_ms": "x"}
    )
    assert normalized == DEFAULT_SPEECH


def test_missing_speech_metadata_uses_defaults():
    assert normalize_speech_labels(None) == DEFAULT_SPEECH


def test_numeric_values_are_clamped_to_safe_ranges():
    # A (mistyped) extreme profile plus extreme labels must still clamp.
    profile = configured_profile(speed=5.0, stability=9.0, similarity_boost=-2.0, style=7.0)
    mapped = map_speech_style(profile, {"emotion": "tearful", "pace": "fast", "energy": "high", "hesitation": "moderate"})
    assert SPEED_RANGE[0] <= mapped.speed <= SPEED_RANGE[1]
    assert STABILITY_RANGE[0] <= mapped.stability <= STABILITY_RANGE[1]
    assert 0.3 <= mapped.similarity_boost <= 1.0
    assert STYLE_RANGE[0] <= mapped.style <= STYLE_RANGE[1]


def test_pause_before_ms_is_clamped():
    profile = configured_profile()
    mapped = map_speech_style(profile, {"pause_before_ms": 99999})
    assert mapped.pause_before_ms == PAUSE_BEFORE_MS_RANGE[1]
    mapped = map_speech_style(profile, {"pause_before_ms": -100})
    assert mapped.pause_before_ms == 0


def test_pace_changes_speed_relative_to_profile():
    profile = configured_profile(speed=0.94)
    slow = map_speech_style(profile, {"pace": "very_slow"})
    fast = map_speech_style(profile, {"pace": "fast"})
    normal = map_speech_style(profile, {"pace": "normal"})
    assert slow.speed < normal.speed < fast.speed
    assert normal.speed == pytest.approx(0.94, abs=0.01)


# ------------------------------------------------------- audio cache
def test_cache_key_includes_all_relevant_settings():
    base = dict(voice_id="v1", model_id="m1", text="hello", voice_settings={"speed": 1.0}, output_format="mp3_44100_128")
    key = make_cache_key(**base)
    assert key == make_cache_key(**base)  # deterministic
    assert key != make_cache_key(**{**base, "voice_id": "v2"})
    assert key != make_cache_key(**{**base, "model_id": "m2"})
    assert key != make_cache_key(**{**base, "text": "different"})
    assert key != make_cache_key(**{**base, "voice_settings": {"speed": 0.9}})
    assert key != make_cache_key(**{**base, "output_format": "mp3_22050_32"})


def test_cache_is_bounded_lru():
    cache = AudioCache(max_entries=2)
    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.get("a")  # touch: "a" becomes most recent
    cache.put("c", b"3")  # evicts "b"
    assert cache.get("a") == b"1"
    assert cache.get("b") is None
    assert cache.get("c") == b"3"
    assert len(cache) == 2


# ------------------------------------------------------- status endpoint
def test_status_unknown_case_is_404(engine):
    with make_voice_client(engine) as client:
        response = client.get("/api/voice/status/not_a_case")
    assert response.status_code == 404


def test_status_missing_api_key_reports_browser_fallback(engine, monkeypatch):
    with make_voice_client(engine, monkeypatch=monkeypatch, api_key="") as client:
        response = client.get("/api/voice/status/carly")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["provider"] == "browser"


def test_status_disabled_reports_browser_fallback(engine, monkeypatch):
    with make_voice_client(engine, monkeypatch=monkeypatch, enabled=False) as client:
        response = client.get("/api/voice/status/carly")
    assert response.json()["available"] is False


def test_status_placeholder_voice_id_counts_as_unconfigured(engine, monkeypatch):
    # PASTE_..._VOICE_ID_HERE placeholders must count as "not configured".
    give_carly_a_placeholder(monkeypatch)
    with make_voice_client(engine, monkeypatch=monkeypatch) as client:
        response = client.get("/api/voice/status/carly")
    body = response.json()
    assert body["available"] is False
    assert body["provider"] == "browser"


def test_status_configured_case_is_available_and_leaks_nothing(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, monkeypatch=monkeypatch) as client:
        response = client.get("/api/voice/status/carly")
    body = response.json()
    assert body["available"] is True
    assert body["provider"] == "elevenlabs"
    text = response.text
    assert "real-voice-id" not in text  # voice ID never exposed
    assert "test-key" not in text  # key never exposed


# --------------------------------------------------- synthesize endpoint
def test_synthesize_invalid_case_is_404(engine, monkeypatch):
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as client:
        response = client.post("/api/voice/synthesize", json=_synth_payload(caseId="nope"))
    assert response.status_code == 404


def test_synthesize_unconfigured_voice_is_409_voice_unavailable(engine, monkeypatch):
    # Placeholder voice id -> the frontend should fall back to browser TTS.
    give_carly_a_placeholder(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as client:
        response = client.post("/api/voice/synthesize", json=_synth_payload())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "voice_unavailable"


def test_synthesize_disabled_is_409(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch, enabled=False) as client:
        response = client.post("/api/voice/synthesize", json=_synth_payload())
    assert response.status_code == 409


def test_synthesize_missing_key_is_409(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch, api_key="") as client:
        response = client.post("/api/voice/synthesize", json=_synth_payload())
    assert response.status_code == 409


def test_synthesize_excessively_long_text_is_422(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    long_text = "a" * (get_settings().elevenlabs_max_text_chars + 1)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as client:
        response = client.post("/api/voice/synthesize", json=_synth_payload(text=long_text))
    assert response.status_code == 422


def test_synthesize_success_streams_audio_with_pause_header(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        sid = _carly_session(client)
        response = client.post("/api/voice/synthesize", json=_synth_payload(sessionId=sid))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"ID3fakemp3audio-bytes"
    assert response.headers["X-Pause-Before-Ms"] == "350"
    # Voice ID came from the backend case profile, not the request.
    assert fake.calls[0]["voice_id"] == "real-voice-id"


def test_synthesize_invalid_speech_labels_use_safe_defaults(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        sid = _carly_session(client)
        payload = _synth_payload(
            sessionId=sid,
            speechStyle={"emotion": "rage", "pace": "hyper", "energy": "x", "hesitation": "??", "pauseBeforeMs": 123456},
        )
        response = client.post("/api/voice/synthesize", json=payload)
    assert response.status_code == 200
    # Invalid labels -> profile baseline (clamped); pause clamped to 1500.
    assert response.headers["X-Pause-Before-Ms"] == "1500"
    settings_sent = fake.calls[0]["voice_settings"]
    assert SPEED_RANGE[0] <= settings_sent["speed"] <= SPEED_RANGE[1]
    assert STABILITY_RANGE[0] <= settings_sent["stability"] <= STABILITY_RANGE[1]


def test_synthesize_upstream_failure_is_safe_502(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(fail=True), monkeypatch) as client:
        sid = _carly_session(client)
        response = client.post("/api/voice/synthesize", json=_synth_payload(sessionId=sid))
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "voice_synthesis_failed"
    # Safe message: no upstream details, key material, or stack traces.
    assert "key" not in body["error"]["message"].lower()
    assert "elevenlabs" not in body["error"]["message"].lower()


def test_synthesize_frontend_cannot_override_voice_id(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        sid = _carly_session(client)
        payload = _synth_payload(sessionId=sid)
        payload["voiceId"] = "attacker-chosen-voice"  # unknown field: ignored
        response = client.post("/api/voice/synthesize", json=payload)
    assert response.status_code == 200
    assert fake.calls[0]["voice_id"] == "real-voice-id"


def test_camden_mother_turn_uses_single_case_file_voice(engine, monkeypatch, fake_client):
    """TEST 4/5/7: for Camden the mother is the sole speaker and the case-file
    voice_profile IS the mother's voice (one source of truth). A committed mother
    turn synthesizes with that voice; the child ('patient') speaker resolves to
    no voice at all, so Camden's child voice can never be synthesized."""
    from app.patient_engine import case_loader
    from app.voice.voice_profile_loader import load_voice_profile

    # The case-file voice_profile is now the MOTHER's single source of truth.
    camden = case_loader.load_case("camden")
    monkeypatch.setattr(camden.voice_profile, "voice_id", "mother-voice-id")

    # There is no usable child voice path: the 'patient' speaker is unavailable.
    assert load_voice_profile("camden", "patient").available is False
    assert load_voice_profile("camden", "caregiver").profile.voice_id == "mother-voice-id"

    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        client.app.dependency_overrides[
            __import__("app.patient_engine.openai_client", fromlist=["get_openai_client"]).get_openai_client
        ] = lambda: fake_client
        session = client.post(
            "/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": "camden"}
        ).json()
        turn = client.post(
            f"/api/interviews/{session['sessionId']}/messages",
            json={"text": "How has his energy been?", "caseId": "camden", "clientTurnId": "cam1", "source": "typed"},
        ).json()
        # TEST 6: the stored turn is the mother.
        assert turn["speakerId"] == "mother" and turn["speakerLabel"] == "Camden's Mother"
        response = client.post(
            "/api/voice/synthesize",
            json=_synth_payload(
                caseId="camden",
                sessionId=session["sessionId"],
                turnId=turn["turnId"],
            ),
        )
        # The mother turn is voiced with the single case-file (mother) voice.
        assert response.status_code == 200
        assert fake.calls[0]["voice_id"] == "mother-voice-id"


def test_synthesize_turn_reference_uses_saved_patient_text(engine, monkeypatch, fake_client):
    """With session+turn ids, the SAVED patient turn is synthesized verbatim -
    mismatched/arbitrary text cannot be voiced through the turn path."""
    give_carly_a_voice_id(monkeypatch)
    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        client.app.dependency_overrides[
            __import__("app.patient_engine.openai_client", fromlist=["get_openai_client"]).get_openai_client
        ] = lambda: fake_client
        session = client.post(
            "/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": "carly"}
        ).json()
        turn = client.post(
            f"/api/interviews/{session['sessionId']}/messages",
            json={"text": "How are you feeling?", "caseId": "carly", "clientTurnId": "t1", "source": "typed"},
        ).json()
        response = client.post(
            "/api/voice/synthesize",
            json=_synth_payload(
                text="attacker text that does not match",
                sessionId=session["sessionId"],
                turnId=turn["turnId"],
            ),
        )
        assert response.status_code == 200
        # The stored patient text was voiced, not the request's text.
        assert fake.calls[0]["text"] == turn["patientText"]

        # A turn id that does not exist is rejected.
        bad = client.post(
            "/api/voice/synthesize",
            json=_synth_payload(sessionId=session["sessionId"], turnId="does-not-exist"),
        )
        assert bad.status_code == 422


def test_synthesize_caches_completed_audio(engine, monkeypatch):
    give_carly_a_voice_id(monkeypatch)
    fake = FakeElevenLabsClient()
    with make_voice_client(engine, fake, monkeypatch) as client:
        sid = _carly_session(client)
        first = client.post("/api/voice/synthesize", json=_synth_payload(sessionId=sid))
        second = client.post("/api/voice/synthesize", json=_synth_payload(sessionId=sid))
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert len(fake.calls) == 1  # second response came from the cache


# ------------------------------------------------- interview speech field
def test_interview_response_includes_normalized_speech(engine):
    from tests.conftest import FakeOpenAIClient

    ok = FakeOpenAIClient(text="I'm doing alright, thanks.")

    # Attach speech metadata to the fake reply.
    from app.schemas.interview_schema import PatientReply, PatientSpeech

    original_generate = ok.generate

    def generate_with_speech(messages, usage_out=None):
        reply: PatientReply = original_generate(messages, usage_out=usage_out)
        reply.speech = PatientSpeech(
            emotion="warm", pace="slow", energy="normal", hesitation="mild", pause_before_ms=9999
        )
        return reply

    ok.generate = generate_with_speech
    with make_client(engine, ok) as client:
        session = client.post(
            "/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": "carly"}
        ).json()
        turn = client.post(
            f"/api/interviews/{session['sessionId']}/messages",
            json={"text": "Hi Carly", "caseId": "carly", "clientTurnId": "t1", "source": "typed"},
        ).json()
    assert turn["speech"]["emotion"] == "warm"
    assert turn["speech"]["pace"] == "slow"
    assert turn["speech"]["pauseBeforeMs"] == 1500  # clamped server-side
    # The transcript text itself is untouched by speech metadata.
    assert turn["patientText"] == "I'm doing alright, thanks."


def test_media_type_mapping():
    assert media_type_for("mp3_44100_128") == "audio/mpeg"
    assert media_type_for("mp3_22050_32") == "audio/mpeg"


# --------------------------------------------- shared HTTP client (latency)
def test_shared_http_client_is_reused_across_calls():
    from app.voice import elevenlabs_client as ec

    ec.close_http_client()
    c1 = ec.get_http_client()
    c2 = ec.get_http_client()
    assert c1 is c2  # keep-alive pool reused between patient turns
    assert not c1.is_closed
    ec.close_http_client()
    assert c1.is_closed
    c3 = ec.get_http_client()  # safe re-creation after close
    assert c3 is not c1
    assert not c3.is_closed
    ec.close_http_client()


def test_lifespan_closes_shared_http_client_on_shutdown(engine):
    from app.voice import elevenlabs_client as ec

    ec.close_http_client()
    with make_client(engine):
        client = ec.get_http_client()
        assert not client.is_closed
    # TestClient context exit runs the FastAPI shutdown lifespan.
    assert client.is_closed


def test_stream_speech_uses_shared_client_and_yields_lazily(monkeypatch):
    """The ElevenLabs wrapper must stream lazily (no full-response buffering)
    and must not create/close a client per call."""
    import httpx

    from app.voice import elevenlabs_client as ec

    pulled: list[bytes] = []
    upstream_closed = {"value": False}

    def upstream_gen():
        try:
            for chunk in (b"a1", b"b2", b"c3", b"d4"):
                pulled.append(chunk)
                yield chunk
        finally:
            upstream_closed["value"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("xi-api-key") == "test-key"
        return httpx.Response(200, content=upstream_gen())

    shared = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ec, "get_http_client", lambda: shared)
    settings = get_settings()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(settings, "elevenlabs_enabled", True)

    client = ec.ElevenLabsClient()
    stream = client.stream_speech(text="hi", voice_id="v", model_id="m", voice_settings={"speed": 1.0})
    first = next(stream)
    assert first == b"a1"
    # Lazy: the upstream was NOT fully consumed to produce the first chunk.
    assert len(pulled) < 4
    # Closing the consumer stops pulling further chunks (no full buffering).
    # (Upstream generator closure through httpx's MockTransport is not
    # guaranteed; real disconnect propagation is covered at the endpoint level
    # in test_endpoint_streams_without_full_buffering_and_never_caches_partials.)
    stream.close()
    assert len(pulled) < 4
    assert not shared.is_closed  # shared client must survive the request
    shared.close()
    _ = upstream_closed  # closure flag; not asserted through MockTransport (see note)


class LazyFakeElevenLabs:
    """Fake that records exactly how many chunks were pulled and whether the
    upstream generator was closed (== stopped reading from ElevenLabs)."""

    def __init__(self, chunks=(b"c1", b"c2", b"c3")):
        self.chunks = list(chunks)
        self.pulled = 0
        self.closed = False
        self.calls: list[dict] = []

    def stream_speech(self, **kwargs):
        self.calls.append(kwargs)
        try:
            for chunk in self.chunks:
                self.pulled += 1
                yield chunk
        finally:
            self.closed = True


def test_endpoint_streams_without_full_buffering_and_never_caches_partials(
    engine, db_session, monkeypatch
):
    """Calls the endpoint function directly to control body consumption:
    - only ONE eager chunk is read before the response starts (error detection)
    - remaining chunks are pulled on demand (true streaming)
    - a client disconnect stops upstream reading and caches nothing."""
    import anyio

    from app.api.voice import synthesize
    from app.schemas.voice_schema import VoiceSynthesizeRequest
    from app.voice.audio_cache import get_audio_cache

    give_carly_a_voice_id(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(settings, "elevenlabs_enabled", True)

    fake = LazyFakeElevenLabs()
    user, sid = seed_owned_session(db_session)
    response = synthesize(
        VoiceSynthesizeRequest(case_id="carly", text="hello there", session_id=sid),
        current_user=user, db=db_session, client=fake,
    )
    # Exactly one chunk was read eagerly (upstream error detection) - the
    # response did NOT buffer the full stream before starting.
    assert fake.pulled == 1
    assert response.headers["content-type"].startswith("audio/mpeg")

    async def consume_two_then_disconnect():
        received = []
        iterator = response.body_iterator
        async for chunk in iterator:
            received.append(chunk)
            if len(received) == 2:
                break
        await iterator.aclose()  # simulated client disconnect
        return received

    received = anyio.run(consume_two_then_disconnect)
    assert received == [b"c1", b"c2"]
    assert fake.pulled == 2  # third chunk never read: upstream reading stopped
    assert fake.closed is True  # upstream generator closed on disconnect
    assert len(get_audio_cache()) == 0  # partial/cancelled audio never cached


def test_endpoint_caches_only_complete_streams(engine, db_session, monkeypatch):
    import anyio

    from app.api.voice import synthesize
    from app.schemas.voice_schema import VoiceSynthesizeRequest
    from app.voice.audio_cache import get_audio_cache

    give_carly_a_voice_id(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(settings, "elevenlabs_enabled", True)

    fake = LazyFakeElevenLabs()
    user, sid = seed_owned_session(db_session)
    response = synthesize(
        VoiceSynthesizeRequest(case_id="carly", text="hello there", session_id=sid),
        current_user=user, db=db_session, client=fake,
    )

    async def consume_all():
        return [chunk async for chunk in response.body_iterator]

    received = anyio.run(consume_all)
    assert b"".join(received) == b"c1c2c3"
    assert len(get_audio_cache()) == 1  # complete stream → cached (bounded LRU)
