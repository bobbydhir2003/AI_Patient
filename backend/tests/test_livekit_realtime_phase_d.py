"""Phase D (native OpenAI voice) - see realtime_session.RealtimeSession.speak /
cancel_active_response / _verbatim_instructions.

No network: the Realtime connection is the Phase A fake, scripted with the GA
response.* audio/transcript lifecycle. These prove OUR wiring (approved text ->
response.create -> audio streamed to the sink -> turn status). The DEFINITIVE
checks - whether Realtime speaks verbatim, audio quality, first-audio latency,
mobile playback - are live-only (a real key + device) and are NOT asserted
here.

speak()/cancel_active_response() are dormant in production (prompt_agent's own
conversation - both spoken and typed - answers via the normal response.*
event flow, never this backend-authored "speak approved text verbatim" path;
see worker.py's _submit_prompt_agent_typed_text and
realtime_prompt_agent.PromptAgentRuntime.submit_typed_text) but are kept as
internal RealtimeSession plumbing for a future phase, so this coverage stays.
"""
import asyncio
import base64

from app.livekit_agent.realtime_session import RealtimeSession
from tests.test_livekit_realtime_phase_a import _FakeClient, _pump_until, _settings


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


def _mk_session(conn):
    return RealtimeSession(
        session_id="s", case_id="carly", identity="stu", track_sid="tr",
        client=_FakeClient(conn), settings=_settings(openai_api_key="sk-x"),
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
