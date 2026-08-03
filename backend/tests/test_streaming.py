"""Tests for the feature-flagged streaming patient-response pipeline."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.exceptions import PatientEngineError
from app.database.connection import get_db, get_db_factory
from app.main import create_app
from app.patient_engine.openai_client import get_openai_client
from app.patient_engine.sentence_stream import SentenceAccumulator
from app.patient_engine.streaming_engine import (
    FirstSentenceRejectedError,
    StreamCompleted,
    StreamSentence,
    StreamSpeech,
    stream_patient_response,
)
from app.schemas.interview_schema import StudentMessageRequest
from app.services import interview_stream_service

from tests.conftest import FakeOpenAIClient


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

def chunked(text: str, size: int = 7) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class FakeStreamingClient(FakeOpenAIClient):
    """OpenAI boundary double that streams scripted deltas."""

    def __init__(self, deltas: list[str], fail_stream: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.deltas = deltas
        self.fail_stream = fail_stream
        self.stream_calls = 0

    def stream_text(self, messages, max_output_tokens=None, usage_out=None):
        self.stream_calls += 1
        self.calls.append(messages)

        def _gen():
            if self.fail_stream:
                raise PatientEngineError("simulated streaming failure")
            for delta in self.deltas:
                yield delta
            if usage_out is not None:
                usage_out.update({"input_tokens": 111, "output_tokens": 42})

        return _gen()


META_TAIL = (
    "\n===META===\n"
    + json.dumps(
        {
            "used_fact_ids": ["carly-cond-01", "not-eligible-id"],
            "response_type": "clinical_answer",
            "supported": True,
            "speech": {
                "emotion": "warm",
                "pace": "slow",
                "energy": "normal",
                "hesitation": "mild",
                "pause_before_ms": 300,
            },
        }
    )
)

TWO_SENTENCES = "I was diagnosed about six months ago. It still feels pretty overwhelming most days."


def collect_events(deltas, case_id="carly", question="Tell me about your condition.", client=None):
    client = client or FakeStreamingClient(deltas)
    gen = stream_patient_response(
        case_id=case_id,
        question=question,
        turns=[],
        disclosed_fact_ids=set(),
        active_topic=None,
        client=client,
    )
    return list(gen), client


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.strip().split("\n")
        name = ""
        data = {}
        for line in lines:
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name:
            events.append((name, data))
    return events


def make_streaming_test_client(engine, openai_client) -> TestClient:
    from fastapi import Depends, Request
    from sqlalchemy.orm import Session as _Session

    from app.dependencies.auth import get_current_user as real_get_current_user
    from app.models import User
    from tests.conftest import seed_default_student

    app = create_app()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_db_factory] = lambda: factory
    app.dependency_overrides[get_openai_client] = lambda: openai_client

    # Auth: the interview endpoints now require a token. The streaming endpoint
    # authorizes ownership by reading the bearer token directly (it must not hold
    # a request-scoped DB session), so we seed a default student, override the
    # dependency-based auth, AND set a real default bearer token on the client so
    # both the dependency path (create_session) and the manual streaming
    # authorizer resolve to the same owning student.
    default_uid = seed_default_student(engine)

    def override_current_user(request: Request, db: _Session = Depends(get_db)) -> User:
        if request.headers.get("Authorization") or request.headers.get("authorization"):
            return real_get_current_user(request, db)
        return db.get(User, default_uid)

    app.dependency_overrides[real_get_current_user] = override_current_user

    tc = TestClient(app)
    token = tc.post(
        "/api/auth/login",
        json={"email": "default@school.edu", "password": "defaultpass1"},
    ).json()["accessToken"]
    tc.headers.update({"Authorization": f"Bearer {token}"})
    return tc


@pytest.fixture()
def streaming_enabled():
    settings = get_settings()
    old = settings.openai_patient_streaming_enabled
    settings.openai_patient_streaming_enabled = True
    yield
    settings.openai_patient_streaming_enabled = old


def create_session(client: TestClient, case_id: str = "carly") -> str:
    resp = client.post(
        "/api/sessions",
        json={"studentName": "Test Student", "studentId": "S1", "caseId": case_id},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["sessionId"]


# ---------------------------------------------------------------------------
# Sentence accumulator
# ---------------------------------------------------------------------------

class TestSentenceAccumulator:
    def feed_all(self, text: str, size: int = 5, min_emit_chars: int = 20):
        acc = SentenceAccumulator(min_emit_chars=min_emit_chars)
        out = []
        for piece in chunked(text, size):
            out.extend(acc.feed(piece))
        tail = acc.flush()
        if tail:
            out.append(tail)
        return out

    def test_basic_split_and_exactly_once(self):
        out = self.feed_all("The first sentence is here. And the second one follows it!")
        assert out == ["The first sentence is here.", "And the second one follows it!"]

    def test_abbreviation_not_split(self):
        out = self.feed_all("I saw Dr. Miller last week about it. She said it looked fine.")
        assert out[0] == "I saw Dr. Miller last week about it."

    def test_decimal_not_split(self):
        out = self.feed_all("The pain is about 3.5 out of ten right now. It gets worse at night.")
        assert out[0] == "The pain is about 3.5 out of ten right now."

    def test_initials_not_split(self):
        out = self.feed_all("My physician is J. Smith over at the clinic. He is very thorough.")
        assert out[0] == "My physician is J. Smith over at the clinic."

    def test_ellipsis_continuing_thought_not_split(self):
        out = self.feed_all("I guess it started... maybe two months ago now. Hard to say exactly.")
        assert out[0] == "I guess it started... maybe two months ago now."

    def test_ellipsis_before_new_sentence_splits(self):
        out = self.feed_all("I honestly don't know what to say... It scares me sometimes at night.")
        assert out == [
            "I honestly don't know what to say...",
            "It scares me sometimes at night.",
        ]

    def test_quoted_question_does_not_split_mid_sentence(self):
        out = self.feed_all('He asked "how bad is it?" and I told him honestly. It was hard.')
        assert out == ['He asked "how bad is it?" and I told him honestly.', "It was hard."]

    def test_short_sentence_merges_with_next(self):
        out = self.feed_all("Yes. It has been getting worse since March though.")
        assert out == ["Yes. It has been getting worse since March though."]

    def test_short_only_answer_flushes(self):
        out = self.feed_all("Yes.")
        assert out == ["Yes."]

    def test_final_partial_sentence_flushes(self):
        out = self.feed_all("It hurts in the morning. And sometimes when I")
        assert out == ["It hurts in the morning.", "And sometimes when I"]

    def test_newline_is_boundary(self):
        out = self.feed_all(
            "It aches most days after my shift ends\nMostly in the evenings after work though."
        )
        assert out[0] == "It aches most days after my shift ends"

    def test_no_duplicate_emission_across_chunk_sizes(self):
        text = "One thing I noticed is fatigue. Another is the stiffness. Mornings are worst."
        for size in (1, 3, 4, 9, 50):
            out = self.feed_all(text, size=size)
            assert out == [
                "One thing I noticed is fatigue.",
                "Another is the stiffness.",
                "Mornings are worst.",
            ], f"chunk size {size}"


# ---------------------------------------------------------------------------
# Streaming engine
# ---------------------------------------------------------------------------

class TestStreamingEngine:
    def test_streams_sentences_then_completes_with_metadata(self):
        events, client = collect_events(chunked(TWO_SENTENCES + META_TAIL))
        assert isinstance(events[0], StreamSpeech)
        sentences = [e for e in events if isinstance(e, StreamSentence)]
        assert [s.text for s in sentences] == [
            "I was diagnosed about six months ago.",
            "It still feels pretty overwhelming most days.",
        ]
        assert [s.index for s in sentences] == [0, 1]
        final = events[-1]
        assert isinstance(final, StreamCompleted)
        assert final.result.text == TWO_SENTENCES
        assert final.metadata_ok is True
        # Only ELIGIBLE fact ids are accepted from the metadata tail.
        assert "not-eligible-id" not in final.result.used_fact_ids
        assert final.result.speech is not None
        assert final.result.speech["pace"] == "slow"
        assert client.stream_calls == 1  # exactly ONE OpenAI request per turn

    def test_metadata_fallback_is_safe(self):
        events, _ = collect_events(chunked(TWO_SENTENCES))  # no META tail at all
        final = events[-1]
        assert isinstance(final, StreamCompleted)
        assert final.metadata_ok is False
        assert final.result.used_fact_ids == []
        assert final.result.newly_disclosed_fact_ids == set()
        assert final.result.response_type == "clinical_answer"
        assert final.result.text == TWO_SENTENCES

    def test_first_sentence_rejected_raises_before_any_speech(self):
        bad = "As an AI language model I cannot really answer that question." + META_TAIL
        with pytest.raises(FirstSentenceRejectedError):
            collect_events(chunked(bad))

    def test_later_bad_sentence_blocked_but_approved_text_kept(self):
        text = (
            "I was diagnosed about six months ago. "
            "As an AI language model I should not say this sentence." + META_TAIL
        )
        events, _ = collect_events(chunked(text))
        sentences = [e.text for e in events if isinstance(e, StreamSentence)]
        assert sentences == ["I was diagnosed about six months ago."]
        final = events[-1]
        assert isinstance(final, StreamCompleted)
        assert final.truncated is True
        assert final.result.text == "I was diagnosed about six months ago."
        assert "AI" not in final.result.text

    def test_fact_id_leak_is_blocked(self):
        text = (
            "I was diagnosed about six months ago. "
            "That is recorded under carly-cond-01 in my facts." + META_TAIL
        )
        events, _ = collect_events(chunked(text))
        sentences = [e.text for e in events if isinstance(e, StreamSentence)]
        assert sentences == ["I was diagnosed about six months ago."]

    def test_delimiter_split_across_deltas(self):
        raw = TWO_SENTENCES + META_TAIL
        events, _ = collect_events(chunked(raw, size=3))
        final = events[-1]
        assert isinstance(final, StreamCompleted)
        assert final.metadata_ok is True
        assert final.result.text == TWO_SENTENCES

    def test_other_case_name_blocked(self):
        text = "I talked to Camden about it yesterday for a while." + META_TAIL
        with pytest.raises(FirstSentenceRejectedError):
            collect_events(chunked(text))


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

class TestStreamingEndpoint:
    def test_disabled_by_default_returns_409(self, engine):
        client_api = make_streaming_test_client(engine, FakeStreamingClient([]))
        with client_api:
            session_id = create_session(client_api)
            resp = client_api.post(
                f"/api/interviews/{session_id}/messages/stream",
                json={"text": "Hi", "caseId": "carly", "clientTurnId": "t1"},
            )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "streaming_disabled"

    def test_full_stream_flow_commits_one_turn_pair(self, engine, streaming_enabled):
        fake = FakeStreamingClient(chunked(TWO_SENTENCES + META_TAIL))
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api)
            resp = client_api.post(
                f"/api/interviews/{session_id}/messages/stream",
                json={
                    "text": "Tell me about your condition.",
                    "caseId": "carly",
                    "clientTurnId": "turn-1",
                    "source": "speech",
                },
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = parse_sse(resp.text)
            names = [n for n, _ in events]
            assert names[0] == "speech"
            assert names.count("final") == 1
            sentence_events = [d for n, d in events if n == "sentence"]
            assert len(sentence_events) == 2
            final = dict(events)["final"]
            assert final["patientText"] == TWO_SENTENCES
            assert final["status"] == "completed"
            assert final["speech"]["pace"] == "slow"
            assert fake.stream_calls == 1

            turns = client_api.get(f"/api/sessions/{session_id}/turns").json()
            assert [t["speaker"] for t in turns] == ["student", "patient"]
            assert turns[1]["content"] == TWO_SENTENCES

    def test_duplicate_client_turn_id_replays_without_regenerating(
        self, engine, streaming_enabled
    ):
        fake = FakeStreamingClient(chunked(TWO_SENTENCES + META_TAIL))
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api)
            body = {
                "text": "Tell me about your condition.",
                "caseId": "carly",
                "clientTurnId": "turn-dup",
            }
            first = client_api.post(f"/api/interviews/{session_id}/messages/stream", json=body)
            assert first.status_code == 200
            second = client_api.post(f"/api/interviews/{session_id}/messages/stream", json=body)
            events = parse_sse(second.text)
            assert [n for n, _ in events] == ["final"]
            assert dict(events)["final"]["patientText"] == TWO_SENTENCES
            assert fake.stream_calls == 1  # replay did NOT regenerate
            turns = client_api.get(f"/api/sessions/{session_id}/turns").json()
            assert len(turns) == 2  # no duplicate rows

    def test_stream_failure_before_first_sentence_persists_nothing(
        self, engine, streaming_enabled
    ):
        fake = FakeStreamingClient([], fail_stream=True)
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api)
            resp = client_api.post(
                f"/api/interviews/{session_id}/messages/stream",
                json={"text": "Hi there.", "caseId": "carly", "clientTurnId": "turn-f"},
            )
            events = parse_sse(resp.text)
            # The early (case-default) speech event may precede the failure;
            # the important guarantees: an error event, NO sentence events.
            assert [n for n, _ in events if n != "speech"] == ["error"]
            assert dict(events)["error"]["code"] == "PATIENT_RESPONSE_UNAVAILABLE"
            turns = client_api.get(f"/api/sessions/{session_id}/turns").json()
            assert turns == []  # student keeps the question; nothing saved

    def test_case_session_mismatch_rejected(self, engine, streaming_enabled):
        fake = FakeStreamingClient(chunked(TWO_SENTENCES + META_TAIL))
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api, case_id="carly")
            resp = client_api.post(
                f"/api/interviews/{session_id}/messages/stream",
                json={"text": "Hi", "caseId": "sofia", "clientTurnId": "turn-x"},
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Interruption: client disconnect commits exactly the emitted sentences
# ---------------------------------------------------------------------------

class TestStreamingInterruption:
    def test_generator_close_after_first_sentence_commits_partial(
        self, engine, streaming_enabled
    ):
        fake = FakeStreamingClient(chunked(TWO_SENTENCES + META_TAIL))
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api)
            factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            payload = StudentMessageRequest(
                text="Tell me about your condition.",
                case_id="carly",
                client_turn_id="turn-int",
                source="speech",
            )
            gen = interview_stream_service.stream_student_message(
                factory, session_id, payload, client=fake
            )
            saw_sentence = False
            for raw in gen:
                if b"event: sentence" in raw:
                    saw_sentence = True
                    break
            assert saw_sentence
            gen.close()  # simulates the browser aborting the SSE (interruption)

            turns = client_api.get(f"/api/sessions/{session_id}/turns").json()
            assert [t["speaker"] for t in turns] == ["student", "patient"]
            assert turns[1]["content"] == "I was diagnosed about six months ago."

    def test_generator_close_before_first_sentence_persists_nothing(
        self, engine, streaming_enabled
    ):
        fake = FakeStreamingClient(chunked(TWO_SENTENCES + META_TAIL))
        client_api = make_streaming_test_client(engine, fake)
        with client_api:
            session_id = create_session(client_api)
            factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            payload = StudentMessageRequest(
                text="Tell me about your condition.",
                case_id="carly",
                client_turn_id="turn-int0",
            )
            gen = interview_stream_service.stream_student_message(
                factory, session_id, payload, client=fake
            )
            next(gen)  # speech event only
            gen.close()
            turns = client_api.get(f"/api/sessions/{session_id}/turns").json()
            assert turns == []
