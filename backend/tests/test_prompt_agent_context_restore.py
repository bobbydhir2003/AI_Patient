"""Phase 3: prior-conversation context restoration (Stop -> Start continuity).

These prove that when a NEW OpenAI Realtime session is created for an interview
that already has prior turns, the runtime seeds that session with the earlier
conversation so the restarted patient understands references to it - WITHOUT
replaying audio, duplicating transcript rows, or re-persisting turns.

Driven with asyncio.run (this suite's convention - no pytest-asyncio dep) and a
fake session, so no network / DB / LiveKit is required.
"""
import asyncio
import types

from app.core.constants import ROLE_PATIENT, ROLE_STUDENT
from app.livekit_agent import realtime_prompt_agent
from app.livekit_agent.realtime_prompt_agent import (
    _MAX_RESTORED_CHARS,
    _MAX_RESTORED_TURNS,
    PromptAgentRuntime,
)


class _FakeSession:
    """Records the client events restore_context sends (same send_event surface
    RealtimeSession exposes)."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.sent.append(event)


def _mk_runtime(**overrides):
    persisted = []
    finals = []
    rt = PromptAgentRuntime(
        session_id=overrides.get("session_id", "s1"),
        case_id="carly",
        config={"model": "gpt-realtime-2.1-mini"},
        db_factory=lambda: (_ for _ in ()).throw(AssertionError("db not expected")),
        on_audio=lambda pcm: None,
        on_student_final=lambda *a: finals.append(("student", a)),
        on_patient_final=lambda *a: finals.append(("patient", a)),
    )
    rt._test_finals = finals  # type: ignore[attr-defined]
    rt._test_persisted = persisted  # type: ignore[attr-defined]
    return rt


def _turn(role: str, content: str):
    return types.SimpleNamespace(role=role, content=content)


class _FakeDb:
    def close(self):
        return None


# ---- restore_context injection behavior -----------------------------------

def test_first_start_empty_transcript_injects_nothing():
    """First voice Start (no prior turns): restore is a fast no-op - no
    conversation items, and behavior is unchanged from today."""

    async def scenario():
        rt = _mk_runtime()
        session = _FakeSession()
        rt.bind_session(session)
        rt._load_prior_turns_sync = lambda: []  # type: ignore[assignment]

        await rt.restore_context()

        assert session.sent == []
        assert rt._context_restored is True
        assert rt._test_finals == []  # nothing persisted / published

    asyncio.run(scenario())


def test_restart_restores_turns_in_chronological_order_with_roles():
    """Stop -> Start with prior turns: each finalized turn is injected as a
    conversation item in chronological order, student->user/input_text and
    patient->assistant/output_text, and NO response.create is ever sent (so no
    historical audio is replayed)."""

    async def scenario():
        rt = _mk_runtime()
        session = _FakeSession()
        rt.bind_session(session)
        rt._load_prior_turns_sync = lambda: [  # type: ignore[assignment]
            (ROLE_STUDENT, "How long have you had the pain?"),
            (ROLE_PATIENT, "About three weeks."),
            (ROLE_STUDENT, "Where exactly is it?"),
            (ROLE_PATIENT, "My lower back."),
        ]

        await rt.restore_context()

        creates = [e for e in session.sent if e["type"] == "conversation.item.create"]
        assert len(creates) == 4
        # No response.create -> no audio generation / replay.
        assert all(e["type"] == "conversation.item.create" for e in session.sent)

        roles = [(e["item"]["role"], e["item"]["content"][0]["type"], e["item"]["content"][0]["text"]) for e in creates]
        assert roles == [
            ("user", "input_text", "How long have you had the pain?"),
            ("assistant", "output_text", "About three weeks."),
            ("user", "input_text", "Where exactly is it?"),
            ("assistant", "output_text", "My lower back."),
        ]
        # Restoration must NOT re-persist or re-publish any turn.
        assert rt._test_finals == []

    asyncio.run(scenario())


def test_restore_runs_only_once_reconnect_does_not_reinject():
    """A normal reconnect reuses the SAME runtime; restore_context must be a
    no-op the second time so history is never duplicated/reinjected."""

    async def scenario():
        rt = _mk_runtime()
        session = _FakeSession()
        rt.bind_session(session)
        calls = {"n": 0}

        def _load():
            calls["n"] += 1
            return [(ROLE_STUDENT, "hi")]

        rt._load_prior_turns_sync = _load  # type: ignore[assignment]

        await rt.restore_context()
        first = len(session.sent)
        await rt.restore_context()  # simulates a reuse; must do nothing new

        assert first == 1
        assert len(session.sent) == 1
        assert calls["n"] == 1

    asyncio.run(scenario())


def test_restore_send_failure_is_fail_open():
    """If the session dies mid-restore, restore_context stops cleanly with the
    partial context accepted and never raises into the caller (voice stays
    usable with whatever context was injected)."""

    async def scenario():
        class _DyingSession:
            def __init__(self):
                self.sent = 0

            async def send_event(self, event):
                self.sent += 1
                if self.sent >= 2:
                    raise RuntimeError("Realtime session is unavailable")

        rt = _mk_runtime()
        session = _DyingSession()
        rt.bind_session(session)
        rt._load_prior_turns_sync = lambda: [  # type: ignore[assignment]
            (ROLE_STUDENT, "q1"),
            (ROLE_PATIENT, "a1"),
            (ROLE_STUDENT, "q2"),
        ]

        # Must not raise.
        await rt.restore_context()
        assert session.sent == 2  # stopped at the failing send
        assert rt._context_restored is True

    asyncio.run(scenario())


def test_restore_noop_without_bound_session():
    async def scenario():
        rt = _mk_runtime()
        rt._load_prior_turns_sync = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("must not load without a session")
        )
        await rt.restore_context()  # _session is None
        assert rt._context_restored is False

    asyncio.run(scenario())


# ---- _load_prior_turns_sync: bounding / cleaning / isolation --------------

class _FakeRepo:
    """Fake TranscriptRepository: returns ONLY the rows registered for the
    exact session_id it is asked about (mirrors the real list_turns' session
    scoping), so a cross-session leak would show up as a failing assertion."""

    _rows: dict[str, list] = {}

    def __init__(self, db):
        self._db = db

    def list_turns(self, session_id: str):
        return list(_FakeRepo._rows.get(session_id, []))


def _with_fake_repo(monkeypatch_rows: dict[str, list]):
    _FakeRepo._rows = monkeypatch_rows
    realtime_prompt_agent.TranscriptRepository = _FakeRepo  # type: ignore[assignment]


def _restore_repo(original):
    realtime_prompt_agent.TranscriptRepository = original  # type: ignore[assignment]


def test_load_skips_malformed_and_preserves_chronological_order():
    original = realtime_prompt_agent.TranscriptRepository
    try:
        _with_fake_repo({
            "s1": [
                _turn(ROLE_STUDENT, "first question"),
                _turn(ROLE_PATIENT, "   "),          # empty -> skipped
                _turn("system", "noise"),             # unknown role -> skipped
                _turn(ROLE_PATIENT, "a real answer"),
                _turn(ROLE_STUDENT, ""),              # empty -> skipped
                _turn(ROLE_STUDENT, "  second question  "),
            ]
        })
        rt = _mk_runtime(session_id="s1")
        # db_factory throws by default; give a harmless one for the sync load.
        rt._db_factory = lambda: _FakeDb()  # type: ignore[assignment]

        turns = rt._load_prior_turns_sync()

        assert turns == [
            (ROLE_STUDENT, "first question"),
            (ROLE_PATIENT, "a real answer"),
            (ROLE_STUDENT, "second question"),
        ]
    finally:
        _restore_repo(original)


def test_load_keeps_only_most_recent_turns():
    original = realtime_prompt_agent.TranscriptRepository
    try:
        rows = [
            _turn(ROLE_STUDENT if i % 2 == 0 else ROLE_PATIENT, f"turn {i}")
            for i in range(_MAX_RESTORED_TURNS + 10)
        ]
        _with_fake_repo({"s1": rows})
        rt = _mk_runtime(session_id="s1")
        rt._db_factory = lambda: _FakeDb()  # type: ignore[assignment]

        turns = rt._load_prior_turns_sync()

        assert len(turns) == _MAX_RESTORED_TURNS
        # Newest tail, chronological: last kept is the final row.
        assert turns[-1][1] == f"turn {_MAX_RESTORED_TURNS + 9}"
        assert turns[0][1] == "turn 10"
    finally:
        _restore_repo(original)


def test_load_enforces_char_ceiling_dropping_oldest_first():
    original = realtime_prompt_agent.TranscriptRepository
    try:
        big = "x" * (_MAX_RESTORED_CHARS // 2)
        _with_fake_repo({
            "s1": [
                _turn(ROLE_STUDENT, big),    # oldest - dropped by char ceiling
                _turn(ROLE_PATIENT, big),
                _turn(ROLE_STUDENT, big),
            ]
        })
        rt = _mk_runtime(session_id="s1")
        rt._db_factory = lambda: _FakeDb()  # type: ignore[assignment]

        turns = rt._load_prior_turns_sync()

        assert sum(len(t) for _r, t in turns) <= _MAX_RESTORED_CHARS
        # The freshest turns survive; the oldest is the one dropped.
        assert len(turns) == 2
    finally:
        _restore_repo(original)


def test_load_is_session_isolated():
    """A runtime for session s1 must NEVER receive another session's turns."""
    original = realtime_prompt_agent.TranscriptRepository
    try:
        _with_fake_repo({
            "s1": [_turn(ROLE_STUDENT, "mine")],
            "s2": [_turn(ROLE_STUDENT, "someone else's")],
        })
        rt = _mk_runtime(session_id="s1")
        rt._db_factory = lambda: _FakeDb()  # type: ignore[assignment]

        turns = rt._load_prior_turns_sync()

        assert turns == [(ROLE_STUDENT, "mine")]
    finally:
        _restore_repo(original)
