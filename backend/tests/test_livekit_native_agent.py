import asyncio
import base64
import json
import threading

import pytest

from app.core.config import Settings
from app.livekit_agent import native_agent
from app.livekit_agent.native_agent_runtime import NativeRealtimeAgentRuntime
from app.livekit_agent.realtime_client import (
    NATIVE_ALLOWED_FACTS_TOOL,
    NATIVE_STAGE_RESPONSE_TOOL,
    build_native_agent_session_update,
)
from app.livekit_agent.realtime_session import RealtimeSession
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from tests.test_livekit_phase_c import _seed_session
from tests.test_livekit_realtime_phase_a import _FakeClient, _pump_until


def _settings(**overrides):
    values = {
        "environment": "development",
        "database_url": "sqlite://",
        "livekit_realtime_engine_enabled": True,
        "openai_api_key": "test-key",
        "openai_realtime_engine_mode": "native_agent",
    }
    values.update(overrides)
    return Settings(**values)


def test_native_agent_flag_off_preserves_controlled_default():
    settings = _settings(livekit_realtime_engine_enabled=False)
    assert settings.realtime_engine_active is False
    assert settings.realtime_native_agent_active is False
    assert Settings(environment="development", database_url="sqlite://").openai_realtime_engine_mode == "controlled"


def test_native_session_registers_only_restricted_tools_and_native_vad():
    payload = build_native_agent_session_update(_settings(), instructions="patient policy")
    session = payload["session"]
    assert session["model"] == "gpt-realtime-2.1-mini"
    assert {tool["name"] for tool in session["tools"]} == {
        NATIVE_ALLOWED_FACTS_TOOL, NATIVE_STAGE_RESPONSE_TOOL,
    }
    assert session["tool_choice"]["name"] == NATIVE_ALLOWED_FACTS_TOOL
    vad = session["audio"]["input"]["turn_detection"]
    assert vad["create_response"] is True
    assert vad["interrupt_response"] is False


class _NativeWireRuntime:
    instructions = "native patient policy"

    def __init__(self):
        self.session = None
        self.events = []

    def bind_session(self, session):
        self.session = session

    async def handle_event(self, event_type, event):
        self.events.append((event_type, event))

    async def submit_typed_text(self, _text, _client_turn_id):
        pass


class _NativeWireConn:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()

    async def send(self, event):
        self.sent.append(event)
        if event["type"] == "session.update":
            self.queue.put_nowait({
                "type": "session.updated",
                "session": {"type": "realtime", "tools": event["session"]["tools"]},
            })

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.queue.put_nowait(None)


def test_native_realtime_session_routes_events_and_targets_active_cancel():
    async def scenario():
        conn = _NativeWireConn()
        runtime = _NativeWireRuntime()
        session = RealtimeSession(
            session_id="s", case_id="carly", identity="student", track_sid="mic",
            client=_FakeClient(conn), settings=_settings(), native_agent=runtime,
        )
        await session.start()
        assert await _pump_until(lambda: session.is_ready)
        assert conn.sent[0]["session"]["tool_choice"]["name"] == NATIVE_ALLOWED_FACTS_TOOL
        session.arm_native_response("resp-a")
        session.note_native_audio("resp-a", "item-assistant-a", 4800)
        assert session.quarantine_active_native_response() == "resp-a"
        assert session.is_native_response_cancelled("resp-a") is True
        await session.cancel_active_response()
        await session.cancel_active_response()
        cancels = [event for event in conn.sent if event["type"] == "response.cancel"]
        assert cancels == [{"type": "response.cancel", "response_id": "resp-a"}]
        truncates = [event for event in conn.sent if event["type"] == "conversation.item.truncate"]
        assert len(truncates) == 1
        assert truncates[0]["item_id"] == "item-assistant-a"
        conn.queue.put_nowait({"type": "response.created", "response": {"id": "resp-b"}})
        assert await _pump_until(lambda: bool(runtime.events))
        await session.aclose()

    asyncio.run(scenario())


def _student_and_authorization(engine, *, question="Where does it hurt?"):
    factory, sid = _seed_session(engine, case_id="carly")
    db = factory()
    try:
        student = native_agent.persist_student_turn_once(
            db, session_id=sid, case_id="carly", client_turn_id="native-1",
            text=question, source="speech",
        )
        authorization = native_agent.authorize_patient_facts(
            db, session_id=sid, case_id="carly", client_turn_id="native-1",
            question=question,
        )
        return factory, sid, student.id, authorization
    finally:
        db.close()


def test_allowed_facts_reuse_existing_disclosure_and_forbidden_fact_absent(engine):
    _factory, _sid, _student_id, authorization = _student_and_authorization(engine)
    ids = authorization.allowed_fact_ids
    assert "carly-pain-01" in ids
    assert "carly-fam-02" not in ids
    assert all(fact.topic in {"symptoms_pain", "condition"} for fact in authorization.facts)


def test_wrong_case_and_arbitrary_fact_mutation_rejected(engine):
    factory, sid, _student_id, authorization = _student_and_authorization(engine)
    db = factory()
    try:
        with pytest.raises(native_agent.NativeAgentAuthorizationError):
            native_agent.authorize_patient_facts(
                db, session_id=sid, case_id="sofia", client_turn_id="native-1",
                question="Where does it hurt?",
            )
    finally:
        db.close()
    with pytest.raises(native_agent.NativeAgentAuthorizationError):
        native_agent.stage_patient_response(
            authorization,
            authorization_id=authorization.authorization_id,
            patient_text="My secret family history is complicated.",
            used_fact_ids=["carly-fam-02"],
        )


def test_patient_persistence_and_disclosure_are_atomic_and_idempotent(engine):
    factory, sid, student_id, authorization = _student_and_authorization(engine)
    staged = native_agent.stage_patient_response(
        authorization,
        authorization_id=authorization.authorization_id,
        patient_text="My wrists hurt about four or five out of ten.",
        used_fact_ids=["carly-pain-01"],
    )
    lock = threading.Lock()
    db = factory()
    try:
        result = native_agent.persist_delivered_patient_turn(
            db, staged=staged,
            delivered_text=staged.patient_text,
            completed=True,
            is_generation_valid=lambda: True,
            generation_authority=lock,
        )
    finally:
        db.close()
    assert result.student_turn_id == student_id
    db = factory()
    try:
        turns = TranscriptRepository(db).list_turns(sid)
        session = SessionRepository(db).get(sid)
        assert [turn.role for turn in turns] == ["student", "patient"]
        assert "carly-pain-01" in SessionRepository(db).get_disclosed_fact_ids(session)
    finally:
        db.close()


def test_stale_response_writes_no_patient_or_disclosure(engine):
    factory, sid, _student_id, authorization = _student_and_authorization(engine)
    staged = native_agent.stage_patient_response(
        authorization,
        authorization_id=authorization.authorization_id,
        patient_text="My wrists hurt about four or five out of ten.",
        used_fact_ids=["carly-pain-01"],
    )
    db = factory()
    try:
        with pytest.raises(native_agent.NativeAgentStaleError):
            native_agent.persist_delivered_patient_turn(
                db, staged=staged, delivered_text=staged.patient_text, completed=True,
                is_generation_valid=lambda: False,
                generation_authority=threading.Lock(),
            )
    finally:
        db.close()
    db = factory()
    try:
        assert [t.role for t in TranscriptRepository(db).list_turns(sid)] == ["student"]
        session = SessionRepository(db).get(sid)
        assert SessionRepository(db).get_disclosed_fact_ids(session) == set()
        assert session.active_topic is None
    finally:
        db.close()


class _FakeRealtimeSession:
    def __init__(self):
        self.events = []
        self.tool_outputs = []
        self.armed = None
        self.cancelled = set()

    async def send_event(self, event):
        self.events.append(event)

    async def send_tool_output(self, **kwargs):
        self.tool_outputs.append(kwargs)

    def arm_native_response(self, response_id):
        self.armed = response_id

    def note_native_audio(self, _response_id, _item_id, _byte_count):
        pass

    def disarm_native_response(self, response_id):
        if self.armed == response_id:
            self.armed = None

    def is_native_response_cancelled(self, response_id):
        return response_id in self.cancelled

    async def cancel_native_response(self, response_id):
        self.cancelled.add(response_id)


def _runtime(engine):
    factory, sid = _seed_session(engine, case_id="carly")
    generation = {"value": 0}
    audio = []
    student = []
    patient = []
    statuses = []

    def reserve(_client_id):
        generation["value"] += 1
        return generation["value"]

    runtime = NativeRealtimeAgentRuntime(
        session_id=sid,
        case_id="carly",
        model_name="gpt-realtime-2.1-mini",
        db_factory=factory,
        reserve_generation=reserve,
        generation_is_current=lambda epoch: epoch == generation["value"],
        generation_authority=threading.Lock(),
        on_audio=lambda pcm: _append_async(audio, pcm),
        on_speaking_started=lambda client_id, text: statuses.append((client_id, "speaking_started")),
        on_patient_final=lambda client_id, epoch, result, reason: patient.append((client_id, result, reason)),
        on_student_persisted=lambda client_id, epoch, text: student.append((client_id, epoch, text)),
        on_status=lambda client_id, status: statuses.append((client_id, status)),
    )
    session = _FakeRealtimeSession()
    runtime.bind_session(session)
    return runtime, session, factory, sid, generation, audio, student, patient, statuses


async def _append_async(target, value):
    target.append(value)


async def _drive_authorize(runtime, session, *, item="item-a", response="resp-auth"):
    await runtime.handle_event("input_audio_buffer.committed", {"item_id": item})
    await runtime.handle_event(
        "conversation.item.input_audio_transcription.completed",
        {"item_id": item, "transcript": "Where does it hurt?"},
    )
    await runtime.handle_event("response.created", {"response": {"id": response}})
    await runtime.handle_event(
        "response.output_item.added",
        {"response_id": response, "item": {"type": "function_call", "call_id": "call-auth", "name": NATIVE_ALLOWED_FACTS_TOOL}},
    )
    await runtime.handle_event(
        "response.function_call_arguments.done",
        {"response_id": response, "call_id": "call-auth", "arguments": "{}"},
    )
    return runtime._turns[item]


def test_native_runtime_persists_each_turn_once_and_rejects_duplicate_tool_call(engine):
    runtime, session, factory, sid, generation, _audio, student, _patient, _statuses = _runtime(engine)

    async def scenario():
        turn = await _drive_authorize(runtime, session)
        await runtime.handle_event(
            "response.function_call_arguments.done",
            {"response_id": "resp-auth", "call_id": "call-auth", "arguments": "{}"},
        )
        return turn

    turn = asyncio.run(scenario())
    assert generation["value"] == 1
    assert len(student) == 1
    assert len(session.tool_outputs) == 1
    assert turn.authorization is not None
    db = factory()
    try:
        assert len(TranscriptRepository(db).list_turns(sid)) == 1
    finally:
        db.close()


def test_native_runtime_end_to_end_audio_and_transcript(engine):
    runtime, session, factory, sid, _generation, audio, _student, patient, statuses = _runtime(engine)

    async def scenario():
        turn = await _drive_authorize(runtime, session)
        fact_id = next(iter(turn.authorization.allowed_fact_ids))
        await runtime.handle_event("response.created", {"response": {"id": "resp-stage", "metadata": {"native_client_turn_id": turn.client_turn_id, "native_phase": "stage"}}})
        await runtime.handle_event(
            "response.output_item.added",
            {"response_id": "resp-stage", "item": {"type": "function_call", "call_id": "call-stage", "name": NATIVE_STAGE_RESPONSE_TOOL}},
        )
        await runtime.handle_event(
            "response.function_call_arguments.done",
            {"response_id": "resp-stage", "call_id": "call-stage", "arguments": json.dumps({
                "authorization_id": turn.authorization.authorization_id,
                "patient_text": "My wrists have been hurting.",
                "used_fact_ids": [fact_id],
            })},
        )
        await runtime.handle_event("response.created", {"response": {"id": "resp-speak", "metadata": {"native_client_turn_id": turn.client_turn_id, "native_phase": "speak"}}})
        await runtime.handle_event("response.output_audio.delta", {"response_id": "resp-speak", "delta": base64.b64encode(b"\x01\x02").decode()})
        await runtime.handle_event("response.output_audio_transcript.done", {"response_id": "resp-speak", "transcript": "My wrists have been hurting."})
        await runtime.handle_event("response.done", {"response": {"id": "resp-speak", "status": "completed"}})

    asyncio.run(scenario())
    assert audio == [b"\x01\x02"]
    assert len(patient) == 1
    assert statuses[-1][1] == "speaking_ended"
    db = factory()
    try:
        assert [t.role for t in TranscriptRepository(db).list_turns(sid)] == ["student", "patient"]
    finally:
        db.close()


def test_typed_input_enters_same_conversation_without_legacy_generation(engine):
    runtime, session, factory, sid, generation, _audio, student, _patient, _statuses = _runtime(engine)
    asyncio.run(runtime.submit_typed_text("Where does it hurt?", "typed-browser-id"))
    assert generation["value"] == 1
    assert len(student) == 1
    assert session.events[0]["type"] == "conversation.item.create"
    assert session.events[0]["item"]["content"][0]["type"] == "input_text"
    assert session.events[1]["type"] == "response.create"
    db = factory()
    try:
        turns = TranscriptRepository(db).list_turns(sid)
        assert turns[0].client_turn_id == "typed-browser-id"
        assert turns[0].source == "typed"
    finally:
        db.close()


def test_interrupted_stale_response_cannot_commit_or_disclose(engine):
    runtime, session, factory, sid, generation, _audio, _student, patient, _statuses = _runtime(engine)

    async def scenario():
        turn = await _drive_authorize(runtime, session)
        fact_id = next(iter(turn.authorization.allowed_fact_ids))
        turn.staged = native_agent.stage_patient_response(
            turn.authorization,
            authorization_id=turn.authorization.authorization_id,
            patient_text="My wrists have been hurting.",
            used_fact_ids=[fact_id],
        )
        turn.response_phases["resp-a"] = "speak"
        runtime._response_turns["resp-a"] = turn
        turn.final_transcript = "My wrists"
        generation["value"] += 1  # accepted newer B owns authority
        await runtime.handle_event("response.done", {"response": {"id": "resp-a", "status": "cancelled"}})

    asyncio.run(scenario())
    assert patient == []
    db = factory()
    try:
        assert [t.role for t in TranscriptRepository(db).list_turns(sid)] == ["student"]
        db_session = SessionRepository(db).get(sid)
        assert SessionRepository(db).get_disclosed_fact_ids(db_session) == set()
    finally:
        db.close()


def test_interrupted_current_response_persists_partial_without_disclosure(engine):
    runtime, session, factory, sid, _generation, _audio, _student, patient, statuses = _runtime(engine)

    async def scenario():
        turn = await _drive_authorize(runtime, session)
        fact_id = next(iter(turn.authorization.allowed_fact_ids))
        turn.staged = native_agent.stage_patient_response(
            turn.authorization,
            authorization_id=turn.authorization.authorization_id,
            patient_text="My wrists have been hurting quite a bit.",
            used_fact_ids=[fact_id],
        )
        turn.response_phases["resp-partial"] = "speak"
        runtime._response_turns["resp-partial"] = turn
        turn.audio_bytes = 320
        turn.final_transcript = "My wrists have been hurting."
        await runtime.handle_event(
            "response.done", {"response": {"id": "resp-partial", "status": "cancelled"}},
        )

    asyncio.run(scenario())
    assert patient[0][2] == "interrupted"
    assert statuses[-1][1] == "interrupted"
    db = factory()
    try:
        turns = TranscriptRepository(db).list_turns(sid)
        assert turns[-1].content == "My wrists have been hurting."
        assert turns[-1].validation_status == "interrupted"
        db_session = SessionRepository(db).get(sid)
        assert SessionRepository(db).get_disclosed_fact_ids(db_session) == set()
    finally:
        db.close()


def test_unknown_tool_is_rejected_without_patient_write(engine):
    runtime, session, factory, sid, _generation, _audio, _student, _patient, statuses = _runtime(engine)

    async def scenario():
        await runtime.handle_event("input_audio_buffer.committed", {"item_id": "item-unknown"})
        await runtime.handle_event(
            "conversation.item.input_audio_transcription.completed",
            {"item_id": "item-unknown", "transcript": "Where does it hurt?"},
        )
        await runtime.handle_event("response.created", {"response": {"id": "resp-unknown"}})
        await runtime.handle_event(
            "response.function_call_arguments.done",
            {"response_id": "resp-unknown", "call_id": "bad-call", "name": "read_database", "arguments": "{}"},
        )

    asyncio.run(scenario())
    assert statuses[-1][1] == "failed"
    assert json.loads(session.tool_outputs[-1]["output"])["allowed"] is False
    db = factory()
    try:
        assert [t.role for t in TranscriptRepository(db).list_turns(sid)] == ["student"]
    finally:
        db.close()


def test_rapid_committed_turns_keep_fifo_response_correlation_and_monotonic_epochs(engine):
    runtime, _session, _factory, _sid, generation, _audio, student, _patient, _statuses = _runtime(engine)

    async def scenario():
        for item, text in (("item-b", "First question?"), ("item-c", "Actually, second question?")):
            await runtime.handle_event("input_audio_buffer.committed", {"item_id": item})
            await runtime.handle_event(
                "conversation.item.input_audio_transcription.completed",
                {"item_id": item, "transcript": text},
            )
        await runtime.handle_event("response.created", {"response": {"id": "resp-b"}})
        await runtime.handle_event("response.created", {"response": {"id": "resp-c"}})

    asyncio.run(scenario())
    assert [entry[1] for entry in student] == [1, 2]
    assert generation["value"] == 2
    assert runtime._response_turns["resp-b"].item_id == "item-b"
    assert runtime._response_turns["resp-c"].item_id == "item-c"
