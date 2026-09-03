"""Phase C (connect the existing patient engine) - see worker.py's
_handle_realtime_turn / _run_realtime_turn.

Proves a completed Realtime student turn runs the SAME authoritative patient
pipeline the legacy path uses (patient_adapter.generate_and_persist_turn ->
generate_patient_response: topic/speaker routing, fact selection, disclosure
gating, validation, persistence) and stops BEFORE audio - no ElevenLabs, no
publish (native voice is Phase D). No network: OpenAI is the repo's
FakeOpenAIClient.
"""
import asyncio

from app.core.constants import ROLE_PATIENT, ROLE_STUDENT
from app.livekit_agent import patient_adapter
from app.repositories.transcript_repository import TranscriptRepository
from tests.conftest import FakeOpenAIClient
from tests.test_livekit_phase_c import _make_ready_session
from tests.test_livekit_poc import _fake_rtc_for_worker


def _turns(session, session_id):
    db = session._session_factory()
    try:
        return TranscriptRepository(db).list_turns(session_id)
    finally:
        db.close()


def test_realtime_turn_runs_authoritative_engine_and_persists_without_audio(monkeypatch, engine):
    """End-to-end through the REAL engine: student + patient turns persisted,
    disclosure updated, and NO audio synthesized in this phase."""
    with _fake_rtc_for_worker():
        session, _room, sid = _make_ready_session(
            engine, monkeypatch, remote_identities={"student-1": object()},
        )
        fake_openai = FakeOpenAIClient(text="I've had this pain for about a week.")
        monkeypatch.setattr("app.patient_engine.get_openai_client", lambda: fake_openai)

        # Phase C must never reach the ElevenLabs/native-voice synth path.
        def _no_audio(*a, **k):
            raise AssertionError("Phase C must not synthesize audio")

        monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm", _no_audio)

        asyncio.run(session._handle_realtime_turn("realtime-c-1", "what brings you in today?"))

        turns = _turns(session, sid)

    roles = [t.role for t in turns]
    assert ROLE_STUDENT in roles and ROLE_PATIENT in roles
    student = next(t for t in turns if t.role == ROLE_STUDENT)
    patient = next(t for t in turns if t.role == ROLE_PATIENT)
    assert student.content == "what brings you in today?"
    assert student.client_turn_id == "realtime-c-1"
    assert patient.content == "I've had this pain for about a week."
    # Patient row carries the engine's validation status (proof the authoritative
    # validator ran before persistence, not a bypass).
    assert patient.validation_status is not None


def test_realtime_turn_funnels_transcript_into_generate_and_persist(monkeypatch, engine):
    """Wiring: the exact transcript + clientTurnId reach the SAME entry point
    the legacy path uses, and no audio is produced."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)

        calls = []

        def fake_generate(db, *, session_id, case_id, question, client_turn_id,
                          on_stage=None, is_generation_valid=None, generation_authority=None):
            calls.append({"question": question, "client_turn_id": client_turn_id, "case_id": case_id})

            class _R:
                patient_turn_id = "pt-1"
                patient_text = "ok"
                voice_key = "patient"
                replayed = False

            return _R()

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", fake_generate)

        def _no_audio(*a, **k):
            raise AssertionError("Phase C must not synthesize audio")

        monkeypatch.setattr(patient_adapter, "synthesize_patient_audio_pcm", _no_audio)

        asyncio.run(session._handle_realtime_turn("realtime-c-2", "does it hurt when you walk?"))

    assert calls == [{
        "question": "does it hurt when you walk?",
        "client_turn_id": "realtime-c-2",
        "case_id": "carly",
    }]


def test_realtime_turn_generation_error_is_contained(monkeypatch, engine):
    """A generation failure is logged and the turn marked completed - never
    raises out of the handler (it runs as a fire-and-forget task)."""
    with _fake_rtc_for_worker():
        session, _room, _sid = _make_ready_session(engine, monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("openai down")

        monkeypatch.setattr(patient_adapter, "generate_and_persist_turn", boom)
        # Must not raise.
        asyncio.run(session._handle_realtime_turn("realtime-c-6", "hello?"))
        assert "realtime-c-6" in session._completed_turn_ids
