"""Fixes for the first live gpt-realtime-2.1 run, plus the PR 5/6 legacy
removal:

FIX 1 - dual-engine guard: browser SpeechRecognition student_text must NOT
        independently trigger a turn - the Realtime audio path already drives
        every spoken turn natively. A MANUAL typed Send is routed to the
        prompt_agent Realtime conversation - see worker.py's _on_data /
        _submit_prompt_agent_typed_text. The legacy _handle_student_turn/
        _run_turn/ElevenLabs/patient_adapter pipeline and the realtime-OFF
        fallback have been removed entirely (PR 6): the worker now requires
        prompt_agent to start at all (see start()'s fail-closed check).

FIX 2 - barge-in WebSocket correction: cancel_active_response no longer sends the
        invalid output_audio_buffer.clear event, and only sends response.cancel
        when a response is genuinely active (response.created seen). See
        realtime_session.cancel_active_response.

Deterministic, no network.
"""
import asyncio

from app.core.config import get_settings
from app.livekit_agent.realtime_session import RealtimeSession
from tests.test_livekit_realtime_phase_a import _settings
from tests.test_livekit_realtime_phase_d import _QueueConn
from tests.test_livekit_phase_c import _control_messages, _make_ready_session, _run_until_idle, _StudentTextPacket
from tests.test_livekit_poc import _fake_rtc_for_worker


# =====================================================================
# FIX 1 / PR 5 - dual-engine suppression + prompt_agent typed-input routing
# =====================================================================

def _enable_realtime(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "livekit_realtime_engine_enabled", True)
    monkeypatch.setattr(s, "openai_api_key", "sk-x")
    assert s.realtime_engine_active is True
    return s


class _FakePromptAgentRuntime:
    def __init__(self):
        self.submitted: list[tuple[str, str]] = []

    async def submit_typed_text(self, client_turn_id, text):
        self.submitted.append((client_turn_id, text))

    async def aclose(self):
        pass


class _FakeReadySession:
    """Stands in for the RealtimeSession start() would otherwise construct -
    avoids needing a real OPENAI_REALTIME_CARLY_PROMPT_ID / network connection
    just to prove the worker's data-packet ROUTING decision."""

    is_ready = True
    close_reason = None

    async def wait_until_ready(self, timeout):
        return True

    async def cancel_active_response(self):
        pass

    async def aclose(self):
        pass


def _spies(monkeypatch, session):
    """Wires a fake prompt_agent runtime (as if a Realtime session were
    already up), replacing _start_prompt_agent_session (not
    _prompt_agent_runtime directly) so it survives session.start()'s own
    realtime-session bring-up, which would otherwise overwrite it with a real
    PromptAgentRuntime (or fail closed without a configured
    OPENAI_REALTIME_CARLY_PROMPT_ID)."""
    runtime = _FakePromptAgentRuntime()

    async def fake_start_prompt_agent_session(settings, identity, track_sid):
        session._prompt_agent_runtime = runtime
        session._realtime_session = _FakeReadySession()
        return session._realtime_session

    monkeypatch.setattr(session, "_start_prompt_agent_session", fake_start_prompt_agent_session)
    return runtime


def test_realtime_active_browser_speech_is_suppressed(monkeypatch, engine):
    """A browser SpeechRecognition SPEECH final never reaches prompt_agent -
    the Realtime audio path owns every spoken turn."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _enable_realtime(monkeypatch)
        runtime = _spies(monkeypatch, session)

        async def drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("hello there", "browser-uuid-1", source="speech_browser"))
            await _run_until_idle()

        asyncio.run(drive())

    assert runtime.submitted == []        # speech is not manually routed either
    # ack still sent, flagged so the browser does not retry
    acks = _control_messages(room, "turn_ack")
    assert any(a.get("clientTurnId") == "browser-uuid-1" and a.get("semanticIgnored") is True for a in acks)


def test_realtime_active_manual_typed_routes_to_prompt_agent(monkeypatch, engine):
    """PR 5: a manual typed Send under prompt_agent is handed straight to
    PromptAgentRuntime.submit_typed_text - the SAME Realtime conversation
    microphone turns use."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _enable_realtime(monkeypatch)
        runtime = _spies(monkeypatch, session)

        async def drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("please continue", "browser-uuid-2", source="manual_typed"))
            await _run_until_idle()

        asyncio.run(drive())

    assert runtime.submitted == [("browser-uuid-2", "please continue")]  # routed to prompt_agent
    # ack sent, and the clientTurnId is tracked so a resend of the same id is a no-op.
    acks = _control_messages(room, "turn_ack")
    assert any(a.get("clientTurnId") == "browser-uuid-2" for a in acks)
    assert "browser-uuid-2" in session._completed_turn_ids


def test_realtime_engine_off_fails_the_job_closed(monkeypatch, engine):
    """PR 6: the realtime-OFF legacy pipeline is gone - if the Realtime
    engine is unavailable, start() shuts the job down rather than falling
    back to any legacy voice path."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        s = get_settings()
        monkeypatch.setattr(s, "livekit_realtime_engine_enabled", False)
        assert s.realtime_engine_active is False
        shutdown_reasons = []
        monkeypatch.setattr(session, "_shutdown_and_signal", lambda reason: shutdown_reasons.append(reason) or asyncio.sleep(0))

        asyncio.run(session.start())

    assert shutdown_reasons == ["realtime_engine_unavailable"]


# =====================================================================
# FIX 2 - cancel_active_response WebSocket correction
# =====================================================================

def _session_with_conn(conn):
    return RealtimeSession(
        session_id="s", case_id="carly", identity="stu", track_sid="tr",
        client=object(), settings=_settings(openai_api_key="sk-x"),
    )


def _mk(conn):
    sess = _session_with_conn(conn)
    sess._conn = conn
    return sess


def test_cancel_with_active_response_sends_one_response_cancel():
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        sess._active_response = asyncio.Queue()
        sess._active_response_id = "resp_1"       # response.created seen
        await sess.cancel_active_response()
        cancels = [e for e in conn.sent if e.get("type") == "response.cancel"]
        assert len(cancels) == 1                                   # (1) exactly one response.cancel
        assert cancels[0] == {"type": "response.cancel", "response_id": "resp_1"}
        assert all(e.get("type") != "output_audio_buffer.clear" for e in conn.sent)  # (3) never sent
        # the waiting speak() loop was signalled
        assert sess._active_response.get_nowait() == ("cancelled", None)

    asyncio.run(scenario())


def test_cancel_with_no_active_response_sends_nothing():
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        sess._active_response = asyncio.Queue()
        sess._active_response_id = None            # no response.created yet
        await sess.cancel_active_response()
        assert [e for e in conn.sent if e.get("type") == "response.cancel"] == []  # (2) none
        assert sess._orphan_response_creates == 1
        assert sess._active_response_cancel_requested is True
        assert all(e.get("type") != "output_audio_buffer.clear" for e in conn.sent)

    asyncio.run(scenario())


def test_duplicate_interruption_is_idempotent():
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        sess._active_response = asyncio.Queue()
        sess._active_response_id = "resp_1"
        await sess.cancel_active_response()
        await sess.cancel_active_response()        # (4) duplicate
        cancels = [e for e in conn.sent if e.get("type") == "response.cancel"]
        assert len(cancels) == 1                   # only one, id consumed after first
        assert cancels[0]["response_id"] == "resp_1"
        assert sess._orphan_response_creates == 0  # duplicate was not mistaken for pre-create cancel
        assert all(e.get("type") != "output_audio_buffer.clear" for e in conn.sent)

    asyncio.run(scenario())


def test_never_sends_output_audio_buffer_clear_in_either_case():
    async def scenario():
        for rid in ("resp_1", None):
            conn = _QueueConn()
            sess = _mk(conn)
            sess._active_response = asyncio.Queue()
            sess._active_response_id = rid
            await sess.cancel_active_response()
            assert all(e.get("type") != "output_audio_buffer.clear" for e in conn.sent)  # (3)

    asyncio.run(scenario())


def test_late_events_for_cancelled_response_are_ignored():
    """(5) a straggler audio/transcript event carrying an OLD response_id is
    dropped by response_id correlation once a DIFFERENT response is active -
    so a cancelled response's late audio can never be published into a newer
    one. (After speak() returns, _active_response is also None, so _handle_event
    would not route at all - this exercises the correlation guard directly.)"""
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        q: "asyncio.Queue" = asyncio.Queue()
        sess._active_response = q
        sess._orphan_response_ids.add("resp_old")  # cancellation of A already requested
        # A can arrive before B's response.created without being mistaken for B.
        sess._route_response_event(
            "response.output_audio.delta",
            {"type": "response.output_audio.delta", "delta": "AAAA", "response_id": "resp_old"},
        )
        sess._route_response_event(
            "response.output_audio_transcript.done",
            {"type": "response.output_audio_transcript.done", "transcript": "stale", "response_id": "resp_old"},
        )
        assert q.empty()

        # B is then identified and remains completely independent of A.
        sess._route_response_event("response.created", {"response": {"id": "resp_new"}})
        sess._route_response_event(
            "response.output_audio.delta",
            {"type": "response.output_audio.delta", "delta": "AAAA", "response_id": "resp_new"},
        )
        assert (await q.get())[0] == "audio"

    asyncio.run(scenario())


def test_cancel_after_response_done_is_a_noop():
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        sess._active_response = asyncio.Queue()
        sess._active_response_id = "resp_done"
        sess._route_response_event(
            "response.done", {"type": "response.done", "response": {"id": "resp_done"}},
        )

        await sess.cancel_active_response()

        assert [e for e in conn.sent if e.get("type") == "response.cancel"] == []
        assert sess._active_response.get_nowait() == ("done", None)
        assert sess._orphan_response_creates == 0

    asyncio.run(scenario())


def test_cancel_before_created_targets_the_late_orphan_once():
    async def scenario():
        conn = _QueueConn()
        sess = _mk(conn)
        sess._active_response = asyncio.Queue()

        await sess.cancel_active_response()
        sess._route_response_event(
            "response.created", {"type": "response.created", "response": {"id": "late_A"}},
        )
        await asyncio.sleep(0)

        cancels = [e for e in conn.sent if e.get("type") == "response.cancel"]
        assert cancels == [{"type": "response.cancel", "response_id": "late_A"}]
        assert "late_A" in sess._orphan_response_ids
        assert not sess._orphan_responses_drained.is_set()

        sess._route_response_event(
            "response.done", {"type": "response.done", "response": {"id": "late_A"}},
        )
        assert sess._orphan_responses_drained.is_set()

    asyncio.run(scenario())


def test_cancel_transport_error_keeps_session_usable_and_rejects_late_pcm():
    class CancelFailConn(_QueueConn):
        async def send(self, event):
            self.sent.append(event)
            if event.get("type") == "response.cancel":
                raise RuntimeError("transport failed")

    async def scenario():
        conn = CancelFailConn()
        sess = _mk(conn)
        first_q: "asyncio.Queue" = asyncio.Queue()
        sess._active_response = first_q
        sess._active_response_id = "resp_A"

        await sess.cancel_active_response()

        assert first_q.get_nowait() == ("cancelled", None)  # local playback loop still stops
        assert sess._orphan_responses_drained.is_set()       # later responses are not blocked
        assert "resp_A" in sess._orphan_response_ids         # but late A data remains rejected

        second_q: "asyncio.Queue" = asyncio.Queue()
        sess._active_response = second_q
        sess._active_response_cancel_requested = False
        sess._route_response_event(
            "response.output_audio.delta",
            {"type": "response.output_audio.delta", "delta": "AAAA", "response_id": "resp_A"},
        )
        assert second_q.empty()
        sess._route_response_event("response.created", {"response": {"id": "resp_B"}})
        sess._route_response_event(
            "response.output_audio.delta",
            {"type": "response.output_audio.delta", "delta": "AAAA", "response_id": "resp_B"},
        )
        assert (await second_q.get())[0] == "audio"

    asyncio.run(scenario())


def test_provider_cancel_error_is_diagnostic_and_releases_next_response(caplog):
    conn = _QueueConn()
    sess = _mk(conn)
    sess._orphan_response_ids.add("resp_A")
    sess._orphan_responses_drained.clear()

    with caplog.at_level("ERROR", logger="app.livekit_agent.realtime"):
        sess._handle_event({
            "type": "error",
            "error": {"code": "response_cancel_not_active", "message": "already complete"},
        })

    assert sess._orphan_responses_drained.is_set()
    assert "resp_A" in sess._orphan_response_ids  # late A events still rejected
    assert any("realtime_response_cancel_error" in record.message for record in caplog.records)
