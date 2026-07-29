"""Two-participant Camden interview: routing, child language, transcript speaker.

Routing is deterministic and unit-tested directly; the end-to-end path is tested
through the real /messages endpoint with the OpenAI boundary faked.
"""
from tests.conftest import make_client
from tests.test_auth import _factory
from tests.conftest import FakeOpenAIClient


# ------------------------------------------------------------ pure routing unit
def test_router_scenarios():
    from app.patient_engine.speaker_router import route
    S = lambda m, prev=None: route(m, previous_speaker=prev).speaker  # noqa: E731
    assert S("Can you tell me what has changed recently?") == "mother"
    assert S("What medications is he currently taking?") == "mother"
    assert S("When did you first notice he was slowing down?") == "mother"
    assert S("Camden, where does it hurt?") == "camden"
    assert S("What do you like to play?") == "camden"
    assert S("Mom, what concerns do you have?") == "mother"
    assert S("Tell me more") == "mother"                       # ambiguous -> mother
    assert S("What medicine is he taking?", prev="camden") == "mother"  # topic overrides
    assert S("When?", prev="camden") == "camden"               # follow-up stays with Camden
    assert S("I would like to hear from both of you. What would help?") == "both"


# ------------------------------------------------------------ child validator unit
def test_child_validator_shortens_and_deflects():
    from app.patient_engine.child_response_validator import validate_child_response
    assert validate_child_response("My legs hurt.").valid is True
    # Clinical language -> deflection to mother.
    d = validate_child_response("The chemotherapy has reduced my endurance significantly.")
    assert d.changed and "mom" in d.text.lower()
    # Over-long -> shortened.
    long = " ".join(["word"] * 60)
    assert len(validate_child_response(long).text.split()) <= 25


# ------------------------------------------------------------ end-to-end helpers
def _camden_client(engine, text="My legs get tired."):
    return make_client(engine, FakeOpenAIClient(text=text))


def _start(c, case="camden"):
    return c.post("/api/sessions", json={"studentName": "T", "studentId": "", "caseId": case}).json()["sessionId"]


def _send(c, sid, text, case="camden"):
    return c.post(f"/api/interviews/{sid}/messages", json={"text": text, "caseId": case})


# ------------------------------------------------------------ end-to-end routing
def test_general_question_answered_by_mother(engine):
    with _camden_client(engine) as c:
        sid = _start(c)
        r = _send(c, sid, "Can you tell me what has changed recently?").json()
        assert r["speakerId"] == "mother"
        assert r["speakerLabel"].lower().startswith("camden")  # "Camden's Mother"
        # transcript stored the speaker
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        patient = [m for m in msgs if m["sender"] == "patient"][0]
        assert patient["speakerId"] == "mother"


def test_direct_child_question_answered_by_camden(engine):
    with _camden_client(engine) as c:
        sid = _start(c)
        r = _send(c, sid, "Camden, do your legs hurt?").json()
        assert r["speakerId"] == "camden"
        assert r["speakerLabel"] == "Camden"


def test_medical_question_routes_to_mother_even_after_camden(engine):
    with _camden_client(engine) as c:
        sid = _start(c)
        _send(c, sid, "Camden, do your legs hurt?")             # Camden active
        r = _send(c, sid, "What medicine is he taking?").json()  # topic overrides
        assert r["speakerId"] == "mother"


def test_camden_answer_is_deflected_when_model_returns_clinical_text(engine):
    # If the model produces clinical text for Camden, the validator deflects it.
    with _camden_client(engine, text="My acute lymphoblastic leukemia treatment reduces my endurance.") as c:
        sid = _start(c)
        r = _send(c, sid, "Camden, how do you feel?").json()
        assert r["speakerId"] == "camden"
        assert "leukemia" not in r["patientText"].lower()
        assert "mom" in r["patientText"].lower()


def test_both_produces_two_ordered_segments(engine):
    with _camden_client(engine, text="I want to play outside.") as c:
        sid = _start(c)
        r = _send(c, sid, "I would like to hear from both of you. What would help?").json()
        segs = r["responses"]
        assert [s["speakerId"] for s in segs] == ["camden", "mother"]
        # two patient turns stored, in order, each with its speaker label
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        patient = [m for m in msgs if m["sender"] == "patient"]
        assert [m["speakerId"] for m in patient] == ["camden", "mother"]


def test_other_cases_are_single_speaker(engine):
    with _camden_client(engine) as c:
        sid = _start(c, case="carly")
        r = _send(c, sid, "Can you tell me what changed?", case="carly").json()
        assert r["speakerId"] == "patient"
        assert r["responses"] in (None, [])


# ------------------------------------------------------------ STREAMING path
import json as _json  # noqa: E402

from tests.test_streaming import (  # noqa: E402
    FakeStreamingClient,
    chunked,
    make_streaming_test_client,
    parse_sse,
    streaming_enabled,  # noqa: F401  (pytest fixture)
)


def _camden_meta():
    return "\n===META===\n" + _json.dumps({
        "used_fact_ids": [], "response_type": "clinical_answer", "supported": True,
        "speech": {"emotion": "neutral", "pace": "normal", "energy": "normal",
                   "hesitation": "none", "pause_before_ms": 150},
    })


def _stream(c, sid, text, reply="My legs hurt."):
    fake = FakeStreamingClient(list(chunked(reply + _camden_meta())))
    # rebuild client each call with fresh deltas
    return c.post(f"/api/interviews/{sid}/messages/stream",
                  json={"text": text, "caseId": "camden", "clientTurnId": text[:20]}), fake


def test_streaming_routes_direct_child_question_to_camden(engine, streaming_enabled):
    fake = FakeStreamingClient(list(chunked("My legs hurt." + _camden_meta())))
    c = make_streaming_test_client(engine, fake)
    with c:
        sid = c.post("/api/sessions", json={"studentName": "T", "studentId": "", "caseId": "camden"}).json()["sessionId"]
        resp = c.post(f"/api/interviews/{sid}/messages/stream",
                      json={"text": "Camden, do your legs hurt?", "caseId": "camden", "clientTurnId": "a1"})
        events = dict((n, d) for n, d in parse_sse(resp.text))
        assert events["speaker"]["speakerId"] == "camden"
        assert events["final"]["speakerId"] == "camden"
        # transcript stored Camden as the speaker
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        assert [m["speakerId"] for m in msgs if m["sender"] == "patient"] == ["camden"]


def test_streaming_routes_medical_question_to_mother(engine, streaming_enabled):
    fake = FakeStreamingClient(list(chunked("He takes his medicine each morning." + _camden_meta())))
    c = make_streaming_test_client(engine, fake)
    with c:
        sid = c.post("/api/sessions", json={"studentName": "T", "studentId": "", "caseId": "camden"}).json()["sessionId"]
        resp = c.post(f"/api/interviews/{sid}/messages/stream",
                      json={"text": "What medications is he taking?", "caseId": "camden", "clientTurnId": "b1"})
        events = dict((n, d) for n, d in parse_sse(resp.text))
        assert events["speaker"]["speakerId"] == "mother"
        assert events["final"]["speakerId"] == "mother"
