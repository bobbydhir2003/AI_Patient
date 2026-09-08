"""Phase C: production LiveKit reliability protocol (agent-ready handshake,
targeted data messages, turn-delivery ACK/idempotency, full worker error
containment, and structured worker telemetry) - see
app/livekit_agent/worker.py's module docstring for the confirmed production
incident this answers, and src/services/livekit/livekitPocEngine.ts for the
matching frontend half of the protocol (covered by scripts/test-livekit-poc.mjs).

Reuses the SAME fake-rtc/fake-room fixtures test_livekit_poc.py already
established (a real native livekit.rtc FFI is unavailable in CI) rather than
duplicating them - see _fake_rtc_for_worker's own docstring there for why
BOTH sys.modules['livekit.rtc'] and the `rtc` attribute on the already-
imported `livekit` package must be swapped.
"""
import asyncio
import itertools
import json
import logging

from app.core.config import get_settings
from app.livekit_agent.worker import AGENT_PARTICIPANT_IDENTITY, PocAgentSession
from tests.test_livekit_poc import _FakeAgentRoom, _fake_rtc_for_worker

_email_counter = itertools.count(1)


def _seed_session(engine, *, case_id="carly"):
    """Like test_voice.seed_owned_session, but with a unique student
    email/number per call - seed_owned_session hardcodes a single fixed
    email, which collides the moment a test needs two independent sessions
    (e.g. proving two concurrent PocAgentSessions never share state)."""
    from sqlalchemy.orm import sessionmaker

    from app.core.security import hash_password
    from app.models import InterviewSession, Student, User

    n = next(_email_counter)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        student = Student(name="Phase C Student", student_number=f"PC{n}", email=f"phasec{n}@school.edu")
        db.add(student)
        db.flush()
        user = User(
            email=f"phasec{n}@school.edu", password_hash=hash_password("x"), full_name="Phase C Student",
            role="student", student_id=student.id, is_active=True,
        )
        db.add(user)
        session = InterviewSession(student_id=student.id, case_id=case_id, case_category="standard")
        db.add(session)
        db.commit()
        session_id = session.id
    finally:
        db.close()
    return factory, session_id


class _StudentTextPacket:
    topic = "student_text"

    def __init__(self, text: str, client_turn_id: str, source: str | None = None):
        payload: dict = {"text": text, "clientTurnId": client_turn_id}
        # source omitted entirely when None - exercises worker.py's own
        # default-to-"speech_browser" fallback for a legacy/pre-Phase-4
        # frontend build that never sends this field at all.
        if source is not None:
            payload["source"] = source
        self.data = json.dumps(payload).encode()


async def _run_until_idle(iterations: int = 30) -> None:
    """Lets every fire-and-forget task (turn_ack publish, the turn-processing
    task itself, status publishes) run to completion - mirrors
    test_livekit_poc.py's _run_one_turn polling loop."""
    for _ in range(iterations):
        await asyncio.sleep(0.02)


def _control_messages(room: _FakeAgentRoom, msg_type: str) -> list[dict]:
    return [
        body for topic, body, _dest in room.local_participant.published_data
        if topic == "agent_control" and body.get("type") == msg_type
    ]


def _turn_statuses(room: _FakeAgentRoom) -> list[dict]:
    return [
        body for topic, body, _dest in room.local_participant.published_data
        if topic == "patient_turn_status"
    ]


def _make_ready_session(engine, monkeypatch, *, remote_identities=None, case_id="carly"):
    """A PocAgentSession backed by a REAL seeded DB session (so
    _verify_session_exists's genuine DB query is exercised, not bypassed) -
    the default fixture for every test in this file except the
    session-not-found one."""
    factory, session_id = _seed_session(engine, case_id=case_id)
    monkeypatch.setattr("app.livekit_agent.worker.get_db_factory", lambda: factory)
    room = _FakeAgentRoom(remote_identities=remote_identities)
    session = PocAgentSession(
        room=room, session_id=session_id, case_id=case_id,
        job_id="job-1", room_id="room-1",
        on_shutdown=lambda reason: None,
    )
    return session, room, session_id


class _FakeRealtimeSessionForStart:
    """Stands in for the RealtimeSession start() would otherwise construct via
    a real OpenAI Realtime WS connection - lets a test drive session.start()
    to completion (agent_ready etc.) without a configured
    OPENAI_REALTIME_CARLY_PROMPT_ID or network connection."""

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


def _enable_realtime_and_fake_session(monkeypatch, session):
    """prompt_agent is the only startable Realtime mode now, and start()
    fails the whole job closed without it (see worker.py) - most tests in
    this module only care about the SHARED protocol (ack/dedup/telemetry/
    isolation), not the Realtime handshake itself, so this bypasses it.
    Also marks the mic producer attached, as if a student track had already
    subscribed (_ingest_student_audio normally does this) - agent_ready is
    gated on both provider-configured AND producer-attached (see worker.py's
    _maybe_send_realtime_agent_ready), and none of these tests care about
    audio ingest specifically."""
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_realtime_engine_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-x")

    async def fake_start_prompt_agent_session(settings, identity, track_sid):
        session._realtime_session = _FakeRealtimeSessionForStart()
        session._realtime_producer_attached = True
        return session._realtime_session

    monkeypatch.setattr(session, "_start_prompt_agent_session", fake_start_prompt_agent_session)


# =================================================================
# Part 1: agent-ready handshake
# =================================================================

async def _start_and_drain(session) -> None:
    """start() now sends agent_ready from a background task
    (_await_realtime_ready) once the Realtime session reports ready, one hop
    further from start() itself than the old synchronous send - idle-drain so
    that task (and its own fire-and-forget publish_data) actually runs before
    the test inspects published_data."""
    await session.start()
    await _run_until_idle()


def test_agent_ready_sent_after_track_published_and_session_verified(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _enable_realtime_and_fake_session(monkeypatch, session)
        asyncio.run(_start_and_drain(session))

    ready_messages = _control_messages(room, "agent_ready")
    assert len(ready_messages) == 1
    # semanticTurnControl is always false now (the experimental pipeline was
    # removed). promptAgent is true - prompt_agent is the only Realtime mode.
    assert ready_messages[0] == {
        "type": "agent_ready", "semanticTurnControl": False, "promptAgent": True,
    }
    # Track publish must happen BEFORE agent_ready is announced - a student
    # must never be told "ready" before there is anything to hear from.
    assert room.local_participant.published_data[-1][1]["type"] == "agent_ready"


def test_agent_ready_never_sent_when_session_does_not_exist(monkeypatch, engine):
    """The genuine (non-bypassed) _verify_session_exists path: a session id
    that was never seeded must fail closed - no agent_ready, explicit
    shutdown, never a guess."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.livekit_agent.worker.get_db_factory", lambda: factory)

    shutdown_reasons: list[str] = []
    with _fake_rtc_for_worker():
        room = _FakeAgentRoom()
        session = PocAgentSession(
            room=room, session_id="never-seeded-session", case_id="carly",
            on_shutdown=lambda reason: shutdown_reasons.append(reason),
        )
        asyncio.run(session.start())

    assert _control_messages(room, "agent_ready") == []
    assert shutdown_reasons == ["session_not_found"]


def test_agent_ready_targets_the_student_identity_when_known(monkeypatch, engine):
    """Part 2: agent->browser messages target the student identity once
    known, instead of blindly broadcasting - the student is typically
    already present when the agent joins (see the module docstring)."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        _enable_realtime_and_fake_session(monkeypatch, session)
        asyncio.run(_start_and_drain(session))

    entries = [
        dest for topic, body, dest in room.local_participant.published_data
        if topic == "agent_control" and body.get("type") == "agent_ready"
    ]
    assert entries == [["student-1"]]


def test_student_identity_learned_via_participant_connected_when_not_yet_present(monkeypatch, engine):
    """Covers the less common ordering: the student joins AFTER this worker
    (e.g. a reconnect) - participant_connected must still pick up their
    identity for later targeted messages."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities=None)
        _enable_realtime_and_fake_session(monkeypatch, session)
        asyncio.run(session.start())
        assert session._student_identity is None

        class _Student:
            identity = "late-student"

        room.emit("participant_connected", _Student())
        assert session._student_identity == "late-student"


def test_participant_connected_ignores_the_agents_own_identity(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _enable_realtime_and_fake_session(monkeypatch, session)
        asyncio.run(session.start())

        class _SelfEcho:
            identity = AGENT_PARTICIPANT_IDENTITY

        room.emit("participant_connected", _SelfEcho())
        assert session._student_identity is None


def test_completed_turn_ids_bounded(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)
        from app.livekit_agent.worker import _MAX_COMPLETED_TURN_IDS

        for i in range(_MAX_COMPLETED_TURN_IDS + 50):
            session._mark_turn_completed(f"turn-{i}")

        assert len(session._completed_turn_ids) == _MAX_COMPLETED_TURN_IDS
        # Oldest evicted first - turn-0 is long gone, the most recent remain.
        assert "turn-0" not in session._completed_turn_ids
        assert f"turn-{_MAX_COMPLETED_TURN_IDS + 49}" in session._completed_turn_ids


def test_bad_payload_never_crashes_the_handler_and_is_logged(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        _enable_realtime_and_fake_session(monkeypatch, session)

        class _BadPacket:
            topic = "student_text"
            data = b"not json"

        async def _drive():
            await session.start()
            with caplog.at_level(logging.WARNING, logger="app.livekit_agent.worker"):
                room.emit("data_received", _BadPacket())
                await _run_until_idle()

        asyncio.run(_drive())  # must not raise

    assert any("livekit_agent_bad_payload" in r.message for r in caplog.records)
    assert _control_messages(room, "turn_ack") == []


# =================================================================
# Part 10 / isolation: multiple concurrent sessions never share state
# =================================================================

def test_two_sessions_have_independent_dedup_and_identity_state(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session_a, room_a, _sid_a = _make_ready_session(engine, monkeypatch, remote_identities={"student-a": object()})
        session_b, room_b, _sid_b = _make_ready_session(engine, monkeypatch, remote_identities={"student-b": object()})
        _enable_realtime_and_fake_session(monkeypatch, session_a)
        _enable_realtime_and_fake_session(monkeypatch, session_b)
        asyncio.run(session_a.start())
        asyncio.run(session_b.start())

        assert session_a._in_flight_turn_ids is not session_b._in_flight_turn_ids
        assert session_a._completed_turn_ids is not session_b._completed_turn_ids
        assert session_a._student_identity == "student-a"
        assert session_b._student_identity == "student-b"

        session_a._in_flight_turn_ids.add("only-in-a")
        assert "only-in-a" not in session_b._in_flight_turn_ids

        session_a._mark_turn_completed("completed-in-a")
        assert "completed-in-a" not in session_b._completed_turn_ids


def test_two_sessions_deliver_turn_ack_to_their_own_room_only(monkeypatch, engine):
    """Typed input (manual_typed) is routed per-session through
    _submit_prompt_agent_typed_text - proves the ack still targets only the
    originating session's room, independent of a second concurrent session."""
    with _fake_rtc_for_worker():
        session_a, room_a, _sid_a = _make_ready_session(engine, monkeypatch, remote_identities={"student-a": object()})
        session_b, room_b, _sid_b = _make_ready_session(engine, monkeypatch, remote_identities={"student-b": object()})
        _enable_realtime_and_fake_session(monkeypatch, session_a)
        _enable_realtime_and_fake_session(monkeypatch, session_b)

        async def _drive():
            await session_a.start()
            await session_b.start()
            room_a.emit("data_received", _StudentTextPacket("Question A", "turn-a", source="manual_typed"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _control_messages(room_a, "turn_ack") == [{"type": "turn_ack", "clientTurnId": "turn-a"}]
    assert _control_messages(room_b, "turn_ack") == []
