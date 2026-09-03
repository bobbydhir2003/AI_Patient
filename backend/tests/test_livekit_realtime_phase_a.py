"""Phase A (POC OpenAI Realtime native-voice engine) - see
app/livekit_agent/realtime_client.py / realtime_session.py and worker.py's
_maybe_start_realtime_session.

Everything here runs WITHOUT a network connection: the OpenAI Realtime
connection is replaced by an in-process fake with the same send/recv/close
contract (RealtimeConnectionLike), so these tests prove the CONFIG correctness
(semantic_vad + create_response=false), audio forwarding, event handling, clean
lifecycle, and default-off isolation - the things a live real-device test can
only confirm AFTER these hold. Live validation (a real OPENAI_API_KEY + mic)
remains a separate manual step.
"""
import asyncio
import base64
from contextlib import asynccontextmanager

import pytest

from app.core.config import Settings, get_settings
from app.livekit_agent.realtime_client import (
    REALTIME_PCM_SAMPLE_RATE,
    build_session_update,
    encode_audio_append,
)
from app.livekit_agent.realtime_session import RealtimeSession
from tests.test_livekit_phase_c import _make_ready_session
from tests.test_livekit_poc import _fake_rtc_for_worker


# =====================================================================
# Config gating
# =====================================================================

def _settings(**overrides) -> Settings:
    base = dict(jwt_secret_key="test-secret-at-least-32-characters-long")
    base.update(overrides)
    return Settings(**base)


def test_realtime_engine_off_by_default():
    assert _settings().realtime_engine_active is False


def test_realtime_engine_requires_flag_and_key():
    # Flag on but no key -> inactive (fail-safe).
    assert _settings(livekit_realtime_engine_enabled=True, openai_api_key="").realtime_engine_active is False
    # Key but flag off -> inactive.
    assert _settings(livekit_realtime_engine_enabled=False, openai_api_key="sk-x").realtime_engine_active is False
    # Both -> active.
    assert _settings(livekit_realtime_engine_enabled=True, openai_api_key="sk-x").realtime_engine_active is True


# =====================================================================
# Pure payload builders - the single most important correctness check
# =====================================================================

def test_session_update_configures_semantic_vad_without_auto_response():
    s = _settings(
        openai_realtime_model="gpt-realtime", openai_realtime_voice="marin",
        openai_realtime_semantic_eagerness="low",
        openai_realtime_transcription_model="gpt-4o-mini-transcribe",
    )
    payload = build_session_update(s)
    assert payload["type"] == "session.update"
    session = payload["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime"
    assert session["output_modalities"] == ["audio"]

    td = session["audio"]["input"]["turn_detection"]
    assert td["type"] == "semantic_vad"
    assert td["eagerness"] == "low"
    # THE core rule: Realtime detects the turn end but never auto-speaks.
    assert td["create_response"] is False
    assert td["interrupt_response"] is False

    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": REALTIME_PCM_SAMPLE_RATE}
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["output"]["format"]["rate"] == REALTIME_PCM_SAMPLE_RATE


def test_encode_audio_append_is_base64_pcm():
    pcm = b"\x01\x02\x03\x04"
    evt = encode_audio_append(pcm)
    assert evt["type"] == "input_audio_buffer.append"
    assert base64.b64decode(evt["audio"]) == pcm


# =====================================================================
# RealtimeSession lifecycle with a fake connection
# =====================================================================

class _FakeConn:
    """Satisfies RealtimeConnectionLike. Records everything sent, replays a
    scripted list of server events, then parks until close()."""

    def __init__(self, scripted_events):
        self.sent = []
        self._events = list(scripted_events)
        self._closed = asyncio.Event()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)

    async def recv(self):
        if self._events:
            return self._events.pop(0)
        await self._closed.wait()
        return None

    async def close(self):
        self.closed = True
        self._closed.set()


class _FakeClient:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def connect(self):
        yield self._conn


async def _pump_until(predicate, *, tries=200, delay=0.005):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return False


def _make_session(conn, on_event=None):
    return RealtimeSession(
        session_id="sess-1", case_id="carly", identity="student-1", track_sid="track-1",
        client=_FakeClient(conn), settings=_settings(openai_api_key="sk-x"), on_event=on_event,
    )


def _session_updated():
    return {"type": "session.updated", "session": {"type": "realtime"}}


def test_session_update_is_sent_before_any_audio():
    async def scenario():
        conn = _FakeConn([_session_updated()])
        session = _make_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())
        # First thing on the wire is always the session.update config.
        assert conn.sent[0]["type"] == "session.update"
        await session.aclose()
        assert conn.closed is True

    asyncio.run(scenario())


def test_student_audio_is_forwarded_as_append_events():
    async def scenario():
        conn = _FakeConn([_session_updated()])
        session = _make_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())
        session.push_audio_bytes(b"\x00\x01" * 160)
        session.push_audio_bytes(b"\x02\x03" * 160)
        assert await _pump_until(
            lambda: sum(1 for e in conn.sent if e["type"] == "input_audio_buffer.append") >= 2
        )
        appends = [e for e in conn.sent if e["type"] == "input_audio_buffer.append"]
        assert base64.b64decode(appends[0]["audio"]) == b"\x00\x01" * 160
        await session.aclose()

    asyncio.run(scenario())


def test_never_sends_response_create_in_phase_a():
    """Phase A must LISTEN only - the backend never asks Realtime to speak."""
    async def scenario():
        conn = _FakeConn([
            _session_updated(),
            {"type": "input_audio_buffer.speech_started"},
            {"type": "input_audio_buffer.speech_stopped"},
            {"type": "input_audio_buffer.committed", "item_id": "item_1"},
        ])
        session = _make_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())
        session.push_audio_bytes(b"\x00\x01" * 160)
        await _pump_until(lambda: False, tries=20)  # let loops settle
        assert all(e["type"] != "response.create" for e in conn.sent)
        await session.aclose()

    asyncio.run(scenario())


def test_turn_taking_events_are_handled_and_hook_fires():
    async def scenario():
        seen = []
        conn = _FakeConn([
            _session_updated(),
            {"type": "input_audio_buffer.speech_started"},
            {"type": "input_audio_buffer.speech_stopped"},
            {"type": "input_audio_buffer.committed", "item_id": "item_1"},
            {"type": "conversation.item.input_audio_transcription.completed",
             "item_id": "item_1", "transcript": "what brings you in today?"},
            {"type": "error", "error": {"message": "benign"}},
        ])
        session = _make_session(conn, on_event=lambda t, e: seen.append(t))
        await session.start()
        assert await _pump_until(lambda: "conversation.item.input_audio_transcription.completed" in seen)
        # All five scripted events observed, error did not crash the loop.
        assert "input_audio_buffer.speech_started" in seen
        assert "input_audio_buffer.speech_stopped" in seen
        assert "input_audio_buffer.committed" in seen
        assert "error" in seen
        await session.aclose()

    asyncio.run(scenario())


def test_aclose_is_idempotent():
    async def scenario():
        conn = _FakeConn([_session_updated()])
        session = _make_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())
        await session.aclose()
        await session.aclose()  # must not raise
        assert conn.closed is True

    asyncio.run(scenario())


def test_session_update_send_does_not_mark_configured_ready():
    async def scenario():
        conn = _FakeConn([])
        session = _make_session(conn)
        await session.start()
        assert await _pump_until(lambda: bool(conn.sent))
        assert conn.sent[0]["type"] == "session.update"
        assert session.is_connected is True
        assert session.is_ready is False
        assert session._configured_ready.is_set() is False
        await session.aclose()

    asyncio.run(scenario())


def test_session_updated_marks_configured_ready():
    async def scenario():
        conn = _FakeConn([_session_updated()])
        session = _make_session(conn)
        await session.start()
        assert await session.wait_until_ready(1.0) is True
        assert session.is_ready is True
        await session.aclose()

    asyncio.run(scenario())


def test_provider_socket_close_clears_readiness_and_reports_failure():
    async def scenario():
        unavailable = []
        conn = _FakeConn([_session_updated(), None])
        session = RealtimeSession(
            session_id="sess-1", case_id="carly", identity="student-1", track_sid="track-1",
            client=_FakeClient(conn), settings=_settings(openai_api_key="sk-x"),
            on_unavailable=unavailable.append,
        )
        await session.start()
        assert await _pump_until(lambda: session.is_closed)
        assert session.is_ready is False
        assert session.failed is True
        assert session.close_reason == "provider_connection_closed"
        assert unavailable == ["provider_connection_closed"]
        await session.aclose()

    asyncio.run(scenario())


# =====================================================================
# Worker gating: legacy path stays intact, fail-safe to None
# =====================================================================

def test_maybe_start_realtime_session_returns_none_when_engine_off(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        s = get_settings()
        monkeypatch.setattr(s, "livekit_realtime_engine_enabled", False)
        result = asyncio.run(session._maybe_start_realtime_session("student-1", "track-1"))
        assert result is None


def test_maybe_start_realtime_session_fails_safe_to_none_on_error(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        s = get_settings()
        monkeypatch.setattr(s, "livekit_realtime_engine_enabled", True)
        monkeypatch.setattr(s, "openai_api_key", "sk-x")

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("construction failure")

        monkeypatch.setattr("app.livekit_agent.realtime_session.RealtimeSession", _Boom)
        result = asyncio.run(session._maybe_start_realtime_session("student-1", "track-1"))
        assert result is None  # never raises into the ingest task


def test_maybe_start_realtime_session_returns_session_when_active(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        s = get_settings()
        monkeypatch.setattr(s, "livekit_realtime_engine_enabled", True)
        monkeypatch.setattr(s, "openai_api_key", "sk-x")

        started = {}

        class _FakeSession:
            input_sample_rate = REALTIME_PCM_SAMPLE_RATE

            def __init__(self, **kwargs):
                started["kwargs"] = kwargs

            async def start(self):
                started["started"] = True

            async def aclose(self):
                started["closed"] = True

        monkeypatch.setattr("app.livekit_agent.realtime_session.RealtimeSession", _FakeSession)
        result = asyncio.run(session._maybe_start_realtime_session("student-1", "track-1"))
        assert isinstance(result, _FakeSession)
        assert started.get("started") is True
        assert started["kwargs"]["case_id"] == "carly"

    asyncio.run(result.aclose())
    assert started.get("closed") is True
