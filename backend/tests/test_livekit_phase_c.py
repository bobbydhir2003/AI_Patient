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

from app.livekit_agent import patient_adapter
from app.livekit_agent.worker import AGENT_PARTICIPANT_IDENTITY, PocAgentSession
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_poc import _FakeAgentRoom, _fake_rtc_for_worker
from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id

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


# =================================================================
# Part 1: agent-ready handshake
# =================================================================

def test_agent_ready_sent_after_track_published_and_session_verified(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        asyncio.run(session.start())

    ready_messages = _control_messages(room, "agent_ready")
    assert len(ready_messages) == 1
    # Phase 4: agent_ready now additionally carries semanticTurnControl -
    # False here since none of the three LIVEKIT_SEMANTIC_TURN_*_ENABLED
    # flags are set in this test (see test_livekit_phase4_semantic_control.py
    # for the control-active case).
    assert ready_messages[0] == {"type": "agent_ready", "semanticTurnControl": False}
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
        asyncio.run(session.start())

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
        asyncio.run(session.start())
        assert session._student_identity is None

        class _Student:
            identity = "late-student"

        room.emit("participant_connected", _Student())
        assert session._student_identity == "late-student"


def test_participant_connected_ignores_the_agents_own_identity(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        asyncio.run(session.start())

        class _SelfEcho:
            identity = AGENT_PARTICIPANT_IDENTITY

        room.emit("participant_connected", _SelfEcho())
        assert session._student_identity is None


# =================================================================
# End-to-end happy path (with real TTS wiring, not just OpenAI)
# =================================================================

def test_full_turn_happy_path_acks_then_speaks_targeted_at_the_student(monkeypatch, engine):
    """Proves the WHOLE pipeline (ack -> OpenAI -> ElevenLabs -> speaking
    audio) still functions end-to-end after Phase C's protocol changes, and
    that every message in the sequence is targeted at the known student
    identity rather than broadcast."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch, remote_identities={"student-1": object()})
        fake_openai = FakeOpenAIClient(text="I've had it for two days.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
        give_carly_a_voice_id(monkeypatch)
        fake_el = FakeElevenLabsClient(chunks=(b"\x01\x02", b"\x03\x04"))
        monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("How long?", "turn-happy"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _control_messages(room, "turn_ack") == [{"type": "turn_ack", "clientTurnId": "turn-happy"}]
    assert _turn_statuses(room) == [
        {"clientTurnId": "turn-happy", "status": "speaking_started"},
        {"clientTurnId": "turn-happy", "status": "speaking_ended"},
    ]
    # Every message this session ever sent was targeted at the student -
    # never a blind broadcast (Part 2).
    all_destinations = [dest for _topic, _body, dest in room.local_participant.published_data]
    assert all(dest == ["student-1"] for dest in all_destinations)


def test_full_turn_happy_path_for_camden_reaches_tts_via_caregiver_voice(monkeypatch, engine):
    """Regression for the Camden-only LiveKit voice-routing bug: end-to-end
    through the REAL worker/PocAgentSession pipeline (not just the adapter
    unit tests in test_livekit_poc.py), proving Camden's first turn now
    reaches speaking_started/speaking_ended instead of "failed", and that
    ElevenLabs was called with Camden's configured CAREGIVER voice id - the
    old default ("patient" voice_key, for which Camden has no configured
    voice) would have short-circuited the whole turn to "failed" before ever
    reaching this call."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(
            engine, monkeypatch, remote_identities={"student-1": object()}, case_id="camden",
        )
        fake_openai = FakeOpenAIClient(text="He's been much more tired than before.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
        fake_el = FakeElevenLabsClient(chunks=(b"\x01\x02", b"\x03\x04"))
        monkeypatch.setattr(patient_adapter, "get_elevenlabs_client", lambda: fake_el)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("What's been going on?", "camden-turn-happy"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _turn_statuses(room) == [
        {"clientTurnId": "camden-turn-happy", "status": "speaking_started"},
        {"clientTurnId": "camden-turn-happy", "status": "speaking_ended"},
    ]
    assert len(fake_el.calls) == 1
    assert fake_el.calls[0]["voice_id"] == "GP1bgf0sjoFuuHkyrg8E"  # camden.json's caregiver voice_id


# =================================================================
# Part 3: turn-ACK protocol
# =================================================================

def test_turn_ack_sent_before_turn_status_for_the_same_turn(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Hi", "turn-1"))
            await _run_until_idle()

        asyncio.run(_drive())

    acks = _control_messages(room, "turn_ack")
    assert acks == [{"type": "turn_ack", "clientTurnId": "turn-1"}]

    topics_in_order = [topic for topic, _body, _dest in room.local_participant.published_data]
    ack_index = next(i for i, t in enumerate(topics_in_order) if t == "agent_control" and
                      room.local_participant.published_data[i][1].get("type") == "turn_ack")
    status_indices = [i for i, t in enumerate(topics_in_order) if t == "patient_turn_status"]
    assert status_indices and ack_index < status_indices[0], "turn_ack must be sent before OpenAI/TTS processing starts"


# =================================================================
# Part 5: idempotency / duplicate protection
# =================================================================

def test_duplicate_turn_while_in_flight_is_acked_but_not_reprocessed(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        generate_calls: list[str] = []

        def fake_generate(db, **kwargs):
            generate_calls.append(kwargs["client_turn_id"])
            raise patient_adapter.LiveKitPocSessionNotFoundError("stop-here")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        async def _drive():
            await session.start()
            # Reserve the slot exactly like _on_data does synchronously on
            # first receipt, simulating "already in flight" without needing
            # a real cross-thread race.
            session._in_flight_turn_ids.add("dup-1")
            room.emit("data_received", _StudentTextPacket("duplicate while busy", "dup-1"))
            await _run_until_idle()

        asyncio.run(_drive())

    acks = _control_messages(room, "turn_ack")
    assert acks == [{"type": "turn_ack", "clientTurnId": "dup-1"}]
    assert generate_calls == [], "a turn already in flight must never trigger a second OpenAI call"


def test_duplicate_turn_after_completion_is_acked_but_not_regenerated(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Same answer.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Hi", "turn-dup"))
            await _run_until_idle()
            # SAME clientTurnId, resent after the first attempt already
            # completed (e.g. the browser's ack-timeout retry firing after a
            # slow-but-successful first attempt).
            room.emit("data_received", _StudentTextPacket("Hi", "turn-dup"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert len(fake_openai.calls) == 1, "a completed turn must never be regenerated on a duplicate"
    acks = _control_messages(room, "turn_ack")
    assert len(acks) == 2, "every receipt is ack'd, even a duplicate-after-completion"
    assert all(a == {"type": "turn_ack", "clientTurnId": "turn-dup"} for a in acks)


def test_busy_dropped_turn_is_not_marked_completed_and_remains_retryable(monkeypatch, engine):
    """A turn dropped purely due to busy/barge-in never actually ran - unlike
    a genuine duplicate-after-completion, a LATER resend of the SAME
    clientTurnId (once the agent is free again) must still be allowed to
    process for real."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)

        async def _drive():
            await session.start()
            async with session._turn_lock:  # simulate an unrelated turn in flight
                room.emit("data_received", _StudentTextPacket("busy", "turn-busy"))
                await _run_until_idle()

        asyncio.run(_drive())

    assert "turn-busy" not in session._completed_turn_ids
    assert "turn-busy" not in session._in_flight_turn_ids


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


# =================================================================
# Part 7: worker error containment
# =================================================================

def test_tts_exception_emits_failed_status_and_structured_log(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        def raise_tts(*, case_id, text, on_stage=None):
            raise RuntimeError("elevenlabs exploded")

        monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm", raise_tts)

        async def _drive():
            await session.start()
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _StudentTextPacket("Hi", "turn-tts-fail"))
                await _run_until_idle()

        asyncio.run(_drive())

    statuses = _turn_statuses(room)
    assert statuses == [{"clientTurnId": "turn-tts-fail", "status": "failed"}]
    assert any(
        "livekit_agent_turn_processing_failed" in r.message and "turn-tts-fail" in r.message
        for r in caplog.records
    )


def test_openai_exception_emits_failed_status_and_structured_log(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)

        def raise_generate(db, **kwargs):
            raise RuntimeError("openai exploded")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", raise_generate)

        async def _drive():
            await session.start()
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _StudentTextPacket("Hi", "turn-openai-fail"))
                await _run_until_idle()

        asyncio.run(_drive())

    statuses = _turn_statuses(room)
    assert statuses == [{"clientTurnId": "turn-openai-fail", "status": "failed"}]
    assert any(
        "livekit_agent_turn_processing_failed" in r.message and "turn-openai-fail" in r.message
        for r in caplog.records
    )
    # A contained exception must still mark the turn completed for dedup
    # purposes (it genuinely ran, unlike a busy-drop) - a resend must be
    # ack'd but not silently retried into a loop of repeated failures.
    assert "turn-openai-fail" in session._completed_turn_ids


def test_no_unhandled_task_exception_escapes_a_turn_failure(monkeypatch, engine):
    """The exact failure mode a prior forensic inspection identified: an
    exception raised after the try/except wrapping only OpenAI generation
    (the pre-Phase-C code) would propagate out of the fire-and-forget
    asyncio.ensure_future task with no patient_turn_status ever sent. Proven
    here by asserting a status IS sent even for a TTS-stage exception -
    equivalent to asserting the task completed instead of dying silently."""
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)
        monkeypatch.setattr(
            patient_adapter, "synthesize_patient_audio_pcm",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Hi", "turn-boom"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _turn_statuses(room) == [{"clientTurnId": "turn-boom", "status": "failed"}]


# =================================================================
# Part 8: worker receive telemetry
# =================================================================

def test_structured_receive_telemetry_includes_required_fields(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _StudentTextPacket("Hi", "turn-telemetry"))
                await _run_until_idle()

        asyncio.run(_drive())

    required_events = [
        "livekit_agent_ready_sent",
        "livekit_agent_student_packet_received",
        "livekit_agent_turn_ack_sent",
        "livekit_agent_turn_processing_started",
    ]
    for event in required_events:
        matching = [r.message for r in caplog.records if r.message.startswith(event)]
        assert matching, f"expected at least one {event} log line"
        line = matching[0]
        assert f"session_id={sid}" in line
        assert "job_id=job-1" in line
        assert "room_id=room-1" in line


def test_duplicate_turn_received_telemetry_logged(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)
        fake_openai = FakeOpenAIClient(text="Hello.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session.start()
            room.emit("data_received", _StudentTextPacket("Hi", "turn-dup2"))
            await _run_until_idle()
            with caplog.at_level(logging.INFO, logger="app.livekit_agent.worker"):
                room.emit("data_received", _StudentTextPacket("Hi", "turn-dup2"))
                await _run_until_idle()

        asyncio.run(_drive())

    assert any(
        "livekit_agent_duplicate_turn_received" in r.message and "turn-dup2" in r.message
        for r in caplog.records
    )


def test_bad_payload_never_crashes_the_handler_and_is_logged(monkeypatch, engine, caplog):
    with _fake_rtc_for_worker():
        session, room, _sid = _make_ready_session(engine, monkeypatch)

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


def test_two_sessions_deliver_status_to_their_own_room_only(monkeypatch, engine):
    with _fake_rtc_for_worker():
        session_a, room_a, _sid_a = _make_ready_session(engine, monkeypatch, remote_identities={"student-a": object()})
        session_b, room_b, _sid_b = _make_ready_session(engine, monkeypatch, remote_identities={"student-b": object()})
        fake_openai = FakeOpenAIClient(text="Independent answer.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        async def _drive():
            await session_a.start()
            await session_b.start()
            room_a.emit("data_received", _StudentTextPacket("Question A", "turn-a"))
            await _run_until_idle()

        asyncio.run(_drive())

    assert _turn_statuses(room_a), "session A's own room must have received its turn's status"
    assert _turn_statuses(room_b) == [], "session B's room must be completely unaffected by session A's turn"
    assert _control_messages(room_a, "turn_ack") == [{"type": "turn_ack", "clientTurnId": "turn-a"}]
    assert _control_messages(room_b, "turn_ack") == []
