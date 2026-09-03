"""Phase B (Realtime as the turn-taking brain) - see
app/livekit_agent/realtime_turn_controller.py and worker.py's
_handle_realtime_turn.

No network: the controller is exercised with synthetic Realtime event sequences
(the SAME event shapes the GA API emits), and the session integration uses the
Phase A in-process fake connection. These prove OUR mapping of events -> exactly
one coherent backend turn (clientTurnId lifecycle, dedup, no phantom). Whether
semantic_vad itself correctly holds a real thinking pause is OpenAI's behavior,
measured live in Phase H - not asserted here.
"""
import asyncio

import pytest

from app.livekit_agent.realtime_session import RealtimeSession
from app.livekit_agent.realtime_turn_controller import RealtimeTurnController
from tests.test_livekit_realtime_phase_a import _FakeClient, _FakeConn, _pump_until, _settings


# =====================================================================
# RealtimeTurnController - event stream -> backend turns
# =====================================================================

def _controller(captured):
    async def on_turn_complete(client_turn_id, transcript):
        captured.append((client_turn_id, transcript))

    return RealtimeTurnController(session_id="sess-1", on_turn_complete=on_turn_complete)


def _speech_started():
    return (_evt("input_audio_buffer.speech_started"))


def _committed(item_id):
    return _evt("input_audio_buffer.committed", item_id=item_id)


def _transcription(item_id, transcript):
    return _evt("conversation.item.input_audio_transcription.completed",
                item_id=item_id, transcript=transcript)


def _evt(t, **kw):
    d = {"type": t}
    d.update(kw)
    return d


def _feed(controller, *events):
    for e in events:
        controller.handle_event(e["type"], e)


def test_one_committed_transcription_makes_one_turn():
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _speech_started(), _committed("item_1"),
              _transcription("item_1", "what brings you in today?"))
        assert await _pump_until(lambda: len(captured) == 1)
        client_turn_id, transcript = captured[0]
        assert client_turn_id == "realtime-sess-1-1"
        assert transcript == "what brings you in today?"

    asyncio.run(scenario())


def test_duplicate_transcription_does_not_double_submit():
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _speech_started(), _committed("item_1"),
              _transcription("item_1", "does it hurt?"),
              _transcription("item_1", "does it hurt?"))  # late duplicate
        assert await _pump_until(lambda: len(captured) == 1)
        await asyncio.sleep(0.02)
        assert len(captured) == 1

    asyncio.run(scenario())


def test_empty_transcript_produces_no_turn():
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _speech_started(), _committed("item_1"), _transcription("item_1", "   "))
        await asyncio.sleep(0.05)
        assert captured == []  # no phantom patient turn

    asyncio.run(scenario())


def test_intra_turn_speech_segments_keep_one_turn_id():
    """Multiple speech_started before a single commit (a held pause split into
    VAD sub-segments) is ONE semantic turn - semantic_vad only commits once."""
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _speech_started(), _speech_started(), _committed("item_1"),
              _transcription("item_1", "when your pain started were you walking or sitting?"))
        assert await _pump_until(lambda: len(captured) == 1)
        assert captured[0][0] == "realtime-sess-1-1"

    asyncio.run(scenario())


def test_two_turns_get_distinct_ids():
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _speech_started(), _committed("item_1"), _transcription("item_1", "first?"))
        assert await _pump_until(lambda: len(captured) == 1)
        _feed(c, _speech_started(), _committed("item_2"), _transcription("item_2", "second?"))
        assert await _pump_until(lambda: len(captured) == 2)
        assert captured[0][0] == "realtime-sess-1-1"
        assert captured[1][0] == "realtime-sess-1-2"
        assert [t for _, t in captured] == ["first?", "second?"]

    asyncio.run(scenario())


def test_transcription_without_prior_speech_started_still_makes_a_turn():
    """Defensive: if speech_started is missed, committed/transcription alone
    still yields a coherent turn rather than dropping it."""
    async def scenario():
        captured = []
        c = _controller(captured)
        _feed(c, _committed("item_9"), _transcription("item_9", "hello?"))
        assert await _pump_until(lambda: len(captured) == 1)
        assert captured[0] == ("realtime-sess-1-1", "hello?")

    asyncio.run(scenario())


# =====================================================================
# RealtimeSession integration - events routed through the controller
# =====================================================================

def test_session_routes_events_to_turn_controller():
    async def scenario():
        captured = []

        async def on_turn_complete(cid, text):
            captured.append((cid, text))

        conn = _FakeConn([
            {"type": "session.updated", "session": {"type": "realtime"}},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "input_audio_buffer.speech_stopped"},
            {"type": "input_audio_buffer.committed", "item_id": "item_1"},
            {"type": "conversation.item.input_audio_transcription.completed",
             "item_id": "item_1", "transcript": "where does it hurt?"},
        ])
        session = RealtimeSession(
            session_id="s", case_id="carly", identity="stu", track_sid="tr",
            client=_FakeClient(conn), settings=_settings(openai_api_key="sk-x"),
            on_turn_complete=on_turn_complete,
        )
        await session.start()
        assert await _pump_until(lambda: len(captured) == 1)
        assert captured[0][1] == "where does it hurt?"
        assert captured[0][0].startswith("realtime-s-")
        await session.aclose()

    asyncio.run(scenario())
