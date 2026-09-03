"""Phase D (native OpenAI voice) - see realtime_session.RealtimeSession.speak /
cancel_active_response / _verbatim_instructions and worker.py's
_run_realtime_turn / _publish_realtime_pcm / _check_voice_fidelity.

No network: the Realtime connection is the Phase A fake, scripted with the GA
response.* audio/transcript lifecycle. These prove OUR wiring (approved text ->
response.create -> audio streamed to the sink -> fidelity comparison -> turn
status). The DEFINITIVE checks - whether Realtime speaks verbatim, audio
quality, first-audio latency, mobile playback - are live-only (a real key +
device) and are NOT asserted here.
"""
import asyncio
import base64
import logging

from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_client import REALTIME_PCM_SAMPLE_RATE
from app.livekit_agent.realtime_session import RealtimeSession, SpeakResult
from app.livekit_agent.worker import _normalize_for_fidelity
from tests.test_livekit_realtime_phase_a import _FakeClient, _pump_until, _settings
from tests.test_livekit_phase_c import _make_ready_session, _turn_statuses
from tests.test_livekit_poc import _fake_rtc_for_worker


class _QueueConn:
    """A fake Realtime connection whose recv() blocks on a queue, so a test can
    push response.* events at runtime AFTER speak() has armed the collector
    (the Phase A _FakeConn parks once its scripted list drains and cannot)."""

    def __init__(self):
        self.sent = []
        self._q: "asyncio.Queue" = asyncio.Queue()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)
        if event.get("type") == "session.update":
            self._q.put_nowait({"type": "session.updated", "session": {"type": "realtime"}})

    async def recv(self):
        return await self._q.get()

    async def close(self):
        self.closed = True
        self._q.put_nowait(None)

    def push(self, event):
        self._q.put_nowait(event)


def _mk_session(conn, on_turn_complete=None):
    return RealtimeSession(
        session_id="s", case_id="carly", identity="stu", track_sid="tr",
        client=_FakeClient(conn), settings=_settings(openai_api_key="sk-x"),
        on_turn_complete=on_turn_complete,
    )


def _audio_delta(pcm: bytes):
    return {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}


# =====================================================================
# RealtimeSession.speak
# =====================================================================

def test_speak_sends_response_create_with_verbatim_instructions():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())

        async def sink(_pcm):
            pass

        # Drive the response to completion once response.create has been sent.
        async def feed():
            await _pump_until(lambda: any(e.get("type") == "response.create" for e in conn.sent))
            conn.push({"type": "response.output_audio_transcript.done", "transcript": "I have had it for two days."})
            conn.push({"type": "response.done"})
        asyncio.ensure_future(feed())
        result = await session.speak(client_turn_id="t1", text="I have had it for two days.", on_audio=sink)

        create = next(e for e in conn.sent if e["type"] == "response.create")
        assert create["response"]["output_modalities"] == ["audio"]
        assert create["response"]["conversation"] == "none"
        assert "I have had it for two days." in create["response"]["instructions"]
        assert result.completed is True
        await session.aclose()

    asyncio.run(scenario())


def test_speak_streams_audio_to_sink_and_returns_transcript():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())

        received = bytearray()

        async def sink(pcm):
            received.extend(pcm)

        async def feed():
            await _pump_until(lambda: any(e.get("type") == "response.create" for e in conn.sent))
            conn.push(_audio_delta(b"\x01\x02" * 100))
            conn.push(_audio_delta(b"\x03\x04" * 100))
            conn.push({"type": "response.output_audio_transcript.done", "transcript": "hello there"})
            conn.push({"type": "response.done"})
        asyncio.ensure_future(feed())
        result = await session.speak(client_turn_id="t1", text="hello there", on_audio=sink)

        assert bytes(received) == b"\x01\x02" * 100 + b"\x03\x04" * 100
        assert result.audio_bytes == 400
        assert result.spoken_transcript == "hello there"
        assert result.completed is True and result.interrupted is False
        await session.aclose()

    asyncio.run(scenario())


def test_speak_empty_text_is_a_noop():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())

        async def sink(_):
            raise AssertionError("no audio for empty text")

        result = await session.speak(client_turn_id="t1", text="   ", on_audio=sink)
        assert result.completed is False and result.audio_bytes == 0
        assert not any(e["type"] == "response.create" for e in conn.sent)
        await session.aclose()

    asyncio.run(scenario())


def test_cancel_active_response_stops_speak_and_sends_cancel():
    async def scenario():
        conn = _QueueConn()
        session = _mk_session(conn)
        await session.start()
        assert await _pump_until(lambda: session._ready.is_set())

        async def sink(_):
            pass

        async def interrupt():
            await _pump_until(lambda: any(e.get("type") == "response.create" for e in conn.sent))
            # Server acknowledges the response, so a cancel is genuinely valid
            # (FIX 2: response.cancel is only sent once a response is active).
            conn.push({"type": "response.created", "response": {"id": "resp_1"}})
            await _pump_until(lambda: session._active_response_id == "resp_1")
            await session.cancel_active_response()

        asyncio.ensure_future(interrupt())
        result = await session.speak(client_turn_id="t1", text="a long answer", on_audio=sink)
        assert result.interrupted is True
        assert {"type": "response.cancel", "response_id": "resp_1"} in conn.sent  # targeted cancel
        assert all(e["type"] != "output_audio_buffer.clear" for e in conn.sent)    # FIX 2: never sent
        await session.aclose()

    asyncio.run(scenario())


# =====================================================================
# Fidelity + normalization
# =====================================================================

def test_normalize_ignores_punctuation_and_case():
    assert _normalize_for_fidelity("I've had it, for 2 days.") == _normalize_for_fidelity("ive had it for 2 days")


def test_fidelity_ok_and_mismatch_logging(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.livekit_agent.worker"):
            session._check_voice_fidelity("t1", "I have had it for two days.", "I have had it for two days")
            session._check_voice_fidelity("t2", "I have had it for two days.", "actually it was a week")
    assert any("realtime_voice_fidelity_mismatch" in r.message for r in caplog.records)
    assert not any(("t1" in r.message and "mismatch" in r.message) for r in caplog.records)


# =====================================================================
# Worker integration: approved text -> native voice, no ElevenLabs
# =====================================================================

def test_run_realtime_turn_speaks_approved_text_without_elevenlabs(monkeypatch, engine):
    with _fake_rtc_for_worker():
        import livekit.rtc as rtc

        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        session._audio_source = rtc.AudioSource(sample_rate=REALTIME_PCM_SAMPLE_RATE, num_channels=1)
        session._patient_audio_sample_rate = REALTIME_PCM_SAMPLE_RATE

        # Real engine approves the response; ElevenLabs must never be reached.
        def fake_generate(db, *, session_id, case_id, question, client_turn_id,
                          on_stage=None, is_generation_valid=None, generation_authority=None):
            class _R:
                patient_turn_id = "pt-1"
                patient_text = "I have had this pain for about a week."
                voice_key = "patient"
                replayed = False
            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)
        monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ElevenLabs in Phase D")))

        spoke = {}

        class _FakeRealtime:
            async def speak(self, *, client_turn_id, text, on_audio):
                spoke["client_turn_id"] = client_turn_id
                spoke["text"] = text
                await on_audio(b"\x00\x01" * 480)  # exercise the 24kHz publisher
                return SpeakResult(spoken_transcript=text, audio_bytes=960, completed=True, interrupted=False)

        session._realtime_session = _FakeRealtime()
        asyncio.run(session._handle_realtime_turn("realtime-d-1", "what brings you in today?"))

    assert spoke["text"] == "I have had this pain for about a week."
    assert spoke["client_turn_id"] == "realtime-d-1"
    statuses = _turn_statuses(room)
    assert {"clientTurnId": "realtime-d-1", "status": "speaking_started"} in statuses
    assert {"clientTurnId": "realtime-d-1", "status": "speaking_ended"} in statuses
