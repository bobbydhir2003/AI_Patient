"""Fixes for the first live gpt-realtime-2.1 run:

FIX 1 - dual-engine guard: when settings.realtime_engine_active is true, browser
        student_text must NOT drive the legacy _handle_student_turn/_run_turn/
        ElevenLabs pipeline (that caused the parallel ElevenLabs 401 -> "Patient
        audio generation failed"). Browser SPEECH is ignored; a MANUAL typed
        Send is routed through the Realtime engine (native voice). See worker.py
        _on_data.

FIX 2 - barge-in WebSocket correction: cancel_active_response no longer sends the
        invalid output_audio_buffer.clear event, and only sends response.cancel
        when a response is genuinely active (response.created seen). See
        realtime_session.cancel_active_response.

Deterministic, no network.
"""
import asyncio

from app.core.config import get_settings
from app.livekit_agent import patient_adapter
from app.livekit_agent.realtime_session import RealtimeSession
from tests.test_livekit_realtime_phase_a import _settings
from tests.test_livekit_realtime_phase_d import _QueueConn
from tests.test_livekit_phase_c import _control_messages, _make_ready_session, _run_until_idle, _StudentTextPacket
from tests.test_livekit_poc import _fake_rtc_for_worker


# =====================================================================
# FIX 1 - dual-engine suppression (worker _on_data)
# =====================================================================

def _enable_realtime(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "livekit_realtime_engine_enabled", True)
    monkeypatch.setattr(s, "openai_api_key", "sk-x")
    assert s.realtime_engine_active is True
    return s


def _spies(monkeypatch, session):
    calls = {"legacy": [], "realtime": [], "eleven": 0}

    async def fake_student_turn(text, cid):
        calls["legacy"].append((text, cid))

    async def fake_realtime_turn(cid, text, my_generation=None, *, reserved=False):
        calls["realtime"].append((cid, text))

    def fake_synth(*a, **k):
        calls["eleven"] += 1
        raise AssertionError("ElevenLabs must not be called on the Realtime path")

    monkeypatch.setattr(session, "_handle_student_turn", fake_student_turn)
    monkeypatch.setattr(session, "_handle_realtime_turn", fake_realtime_turn)
    monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm", fake_synth)
    return calls


def test_realtime_active_browser_speech_is_suppressed(monkeypatch, engine):
    """(1) legacy _run_turn NOT called, (2) ElevenLabs NOT called, (3) the
    Realtime path is untouched by this browser SPEECH packet."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _enable_realtime(monkeypatch)
        calls = _spies(monkeypatch, session)

        async def drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("hello there", "browser-uuid-1", source="speech_browser"))
            await _run_until_idle()

        asyncio.run(drive())

    assert calls["legacy"] == []       # legacy _handle_student_turn/_run_turn NOT called
    assert calls["realtime"] == []      # speech is not manually routed either
    assert calls["eleven"] == 0         # ElevenLabs NOT called
    # ack still sent, flagged so the browser does not retry
    acks = _control_messages(room, "turn_ack")
    assert any(a.get("clientTurnId") == "browser-uuid-1" and a.get("semanticIgnored") is True for a in acks)


def test_realtime_active_manual_typed_routes_to_realtime(monkeypatch, engine):
    """(5) manual typed Send under Realtime is honored via the Realtime engine
    (native voice), NEVER the legacy ElevenLabs path."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _enable_realtime(monkeypatch)
        calls = _spies(monkeypatch, session)

        async def drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("please continue", "browser-uuid-2", source="manual_typed"))
            await _run_until_idle()

        asyncio.run(drive())

    assert calls["realtime"] == [("browser-uuid-2", "please continue")]  # routed to Realtime engine
    assert calls["legacy"] == []                                          # never the legacy path
    assert calls["eleven"] == 0


def test_legacy_mode_still_processes_browser_text(monkeypatch, engine):
    """(4) with the Realtime engine OFF, browser student_text drives the legacy
    pipeline exactly as before."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        s = get_settings()
        monkeypatch.setattr(s, "livekit_realtime_engine_enabled", False)  # legacy mode
        assert s.realtime_engine_active is False
        calls = _spies(monkeypatch, session)

        async def drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("how long?", "browser-uuid-3", source="speech_browser"))
            await _run_until_idle()

        asyncio.run(drive())

    assert calls["legacy"] == [("how long?", "browser-uuid-3")]  # legacy path used, as before
    assert calls["realtime"] == []


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
