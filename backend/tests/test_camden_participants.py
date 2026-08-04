"""Camden interview: the MOTHER is the sole speaker (Dr. Dexter's requirement).

Camden is 4 years old; his mother answers every interview question about him as
his primary caregiver/historian. Routing is deterministic and unit-tested
directly; the end-to-end path is tested through the real /messages and
/messages/stream endpoints with the OpenAI boundary faked. The generic dynamic
`route()` helper is preserved for any future multi-participant case, but Camden
always resolves to the mother.
"""
from tests.conftest import FakeOpenAIClient, make_client


# ----------------------------------------------- caregiver-primary routing unit
def test_camden_always_routes_to_mother():
    """Every normal interview turn for Camden resolves to the mother, regardless
    of wording - general, medical, direct-to-child, or 'both'."""
    from app.patient_engine import case_loader, speaker_router

    case = case_loader.load_case("camden")
    R = lambda m: speaker_router.resolve_for_case(case, m, []).speaker  # noqa: E731
    assert R("Can you tell me what has changed recently?") == "mother"       # TEST 1
    assert R("What medications is he taking?") == "mother"                    # TEST 2
    assert R("Camden, where does it hurt?") == "mother"                      # TEST 3
    assert R("Camden, what do you like to play?") == "mother"               # TEST 4
    assert R("I would like to hear from both of you.") == "mother"          # TEST 5
    assert R("When did you first notice he was slowing down?") == "mother"
    # Never Camden, and never a joint "both" response.
    assert R("What do you like to play?") == "mother"


def test_generic_router_preserved_for_non_locked_cases():
    """The generic dynamic router is untouched (still importable/usable) so a
    future multi-participant case without the caregiver lock could use it."""
    from app.patient_engine.speaker_router import route

    assert route("Camden, where does it hurt?").speaker == "camden"
    assert route("What medications is he taking?").speaker == "mother"


# ------------------------------------------------------------ child validator unit
def test_child_validator_infrastructure_still_present():
    """The child validator remains as safe infrastructure (unused by Camden's
    normal flow now, but kept so nothing that imports it breaks)."""
    from app.patient_engine.child_response_validator import validate_child_response

    assert validate_child_response("My legs hurt.").valid is True
    d = validate_child_response("The chemotherapy has reduced my endurance significantly.")
    assert d.changed and "mom" in d.text.lower()


# ------------------------------------------------------------ end-to-end helpers
def _camden_client(engine, text="He's been much more tired than before."):
    return make_client(engine, FakeOpenAIClient(text=text))


def _start(c, case="camden"):
    return c.post("/api/sessions", json={"studentName": "T", "studentId": "", "caseId": case}).json()["sessionId"]


def _send(c, sid, text, case="camden"):
    return c.post(f"/api/interviews/{sid}/messages", json={"text": text, "caseId": case})


def _assert_mother(r):
    assert r["speakerId"] == "mother"
    assert r["speakerLabel"] == "Camden's Mother"
    # A single mother segment (never a separate Camden response).
    assert [s["speakerId"] for s in (r.get("responses") or [])] == ["mother"]


# --------------------------------------------------- end-to-end (non-streaming)
def test_general_question_answered_by_mother(engine):  # TEST 1
    with _camden_client(engine) as c:
        sid = _start(c)
        r = _send(c, sid, "Can you tell me what has changed recently?").json()
        _assert_mother(r)
        # TEST 6: transcript stored the mother as the speaker + label.
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        patient = [m for m in msgs if m["sender"] == "patient"]
        assert len(patient) == 1
        assert patient[0]["speakerId"] == "mother"
        assert patient[0]["speakerLabel"] == "Camden's Mother"


def test_medical_question_answered_by_mother(engine):  # TEST 2
    with _camden_client(engine) as c:
        sid = _start(c)
        _assert_mother(_send(c, sid, "What medications is he taking?").json())


def test_direct_child_question_still_answered_by_mother(engine):  # TEST 3
    with _camden_client(engine) as c:
        sid = _start(c)
        _assert_mother(_send(c, sid, "Camden, where does it hurt?").json())


def test_direct_child_play_question_answered_by_mother(engine):  # TEST 4
    with _camden_client(engine) as c:
        sid = _start(c)
        _assert_mother(_send(c, sid, "Camden, what do you like to play?").json())


def test_both_request_keeps_mother_single_response(engine):  # TEST 5
    with _camden_client(engine) as c:
        sid = _start(c)
        r = _send(c, sid, "I'd like to hear from both of you.").json()
        _assert_mother(r)
        # Exactly ONE patient turn is stored (no separate Camden response).
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
        assert len([m for m in msgs if m["sender"] == "patient"]) == 1


def test_other_cases_are_single_speaker(engine):  # TEST 8 (regression)
    with _camden_client(engine) as c:
        sid = _start(c, case="carly")
        r = _send(c, sid, "Can you tell me what changed?", case="carly").json()
        assert r["speakerId"] == "patient"
        assert r["responses"] in (None, [])


# ------------------------------------------------------------ STREAMING path (TEST 10)
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


def _run_stream(engine, question, reply):
    fake = FakeStreamingClient(list(chunked(reply + _camden_meta())))
    c = make_streaming_test_client(engine, fake)
    with c:
        sid = c.post("/api/sessions", json={"studentName": "T", "studentId": "", "caseId": "camden"}).json()["sessionId"]
        resp = c.post(f"/api/interviews/{sid}/messages/stream",
                      json={"text": question, "caseId": "camden", "clientTurnId": question[:16]})
        events = dict((n, d) for n, d in parse_sse(resp.text))
        msgs = c.get(f"/api/sessions/{sid}").json()["messages"]
    return events, msgs


def test_streaming_direct_child_question_routes_to_mother(engine, streaming_enabled):  # TEST 3 + 10
    events, msgs = _run_stream(engine, "Camden, where does it hurt?",
                               "He usually tells me his legs and bones hurt the most.")
    assert events["speaker"]["speakerId"] == "mother"
    assert events["speaker"]["speakerLabel"] == "Camden's Mother"
    assert events["final"]["speakerId"] == "mother"
    assert [m["speakerId"] for m in msgs if m["sender"] == "patient"] == ["mother"]


def test_streaming_medical_question_routes_to_mother(engine, streaming_enabled):  # TEST 2 + 10
    events, _ = _run_stream(engine, "What medications is he taking?",
                            "He takes his medicines each morning.")
    assert events["speaker"]["speakerId"] == "mother"
    assert events["final"]["speakerId"] == "mother"
