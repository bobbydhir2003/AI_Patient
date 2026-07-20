"""Transcript persistence: /turns endpoints, idempotency, locking, completion."""


def _session(client, case_id="camden"):
    return client.post(
        "/api/sessions", json={"studentName": "T", "studentId": "", "caseId": case_id}
    ).json()["sessionId"]


def _turn(client, session_id, client_turn_id="ct-1", speaker="student",
          content="What brings you in today?", source="typed"):
    return client.post(
        f"/api/sessions/{session_id}/turns",
        json={"clientTurnId": client_turn_id, "speaker": speaker, "content": content, "source": source},
    )


def test_save_student_and_patient_turns_in_order(client):
    sid = _session(client)
    r1 = _turn(client, sid, "ct-1", "student", "Hello there")
    assert r1.status_code == 201
    r2 = _turn(client, sid, "ct-2", "patient", "Hi... I'm Camden.", source="openai")
    assert r2.status_code == 201
    turns = client.get(f"/api/sessions/{sid}/turns").json()
    assert [t["speaker"] for t in turns] == ["student", "patient"]
    assert [t["turnIndex"] for t in turns] == [0, 1]
    assert turns[0]["clientTurnId"] == "ct-1"
    assert turns[0]["source"] == "typed"


def test_duplicate_client_turn_id_is_idempotent(client):
    sid = _session(client)
    first = _turn(client, sid, "ct-dup").json()
    again = _turn(client, sid, "ct-dup").json()
    assert again["id"] == first["id"]
    assert again["turnIndex"] == first["turnIndex"]
    turns = client.get(f"/api/sessions/{sid}/turns").json()
    assert len(turns) == 1  # no duplicate row, no double turn-index increment


def test_empty_content_rejected(client):
    sid = _session(client)
    r = client.post(
        f"/api/sessions/{sid}/turns",
        json={"clientTurnId": "ct-x", "speaker": "student", "content": "", "source": "typed"},
    )
    assert r.status_code == 422


def test_unknown_session_404(client):
    assert _turn(client, "nope").status_code == 404
    assert client.get("/api/sessions/nope/turns").status_code == 404


def test_completed_session_rejects_new_turns(client):
    sid = _session(client)
    _turn(client, sid, "ct-1", "student", "Hello")
    _turn(client, sid, "ct-2", "patient", "Hi.", source="openai")
    client.post(f"/api/sessions/{sid}/complete")
    r = _turn(client, sid, "ct-3", "student", "One more?")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "transcript_locked"
    assert "locked because the session is completed" in r.json()["error"]["message"]


def test_completion_requires_usable_transcript(client):
    sid = _session(client)
    r = client.post(f"/api/sessions/{sid}/complete")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "transcript_empty"
    # student turn only: still not usable
    _turn(client, sid, "ct-1", "student", "Hello?")
    assert client.post(f"/api/sessions/{sid}/complete").status_code == 409
    # add a patient turn: completion succeeds and is idempotent
    _turn(client, sid, "ct-2", "patient", "Hi.", source="openai")
    done = client.post(f"/api/sessions/{sid}/complete")
    assert done.status_code == 200 and done.json()["locked"] is True
    assert client.post(f"/api/sessions/{sid}/complete").status_code == 200


def test_exchange_endpoint_is_idempotent_by_client_turn_id(client, fake_client):
    sid = _session(client)
    body = {"text": "Hi Camden!", "caseId": "camden", "clientTurnId": "ex-1", "source": "typed"}
    first = client.post(f"/api/interviews/{sid}/messages", json=body).json()
    calls = len(fake_client.calls)
    replay = client.post(f"/api/interviews/{sid}/messages", json=body).json()
    assert replay["turnId"] == first["turnId"]
    assert replay["patientText"] == first["patientText"]
    assert len(fake_client.calls) == calls, "replay must not call OpenAI again"
    turns = client.get(f"/api/sessions/{sid}/turns").json()
    assert len(turns) == 2  # exactly one exchange persisted


def test_transcript_restore_after_reload(client):
    sid = _session(client, case_id="jayden")
    client.post(
        f"/api/interviews/{sid}/messages",
        json={"text": "Tell me about your running.", "caseId": "jayden", "clientTurnId": "r-1"},
    )
    # simulate page reload: session + turns fetched fresh from the backend
    session = client.get(f"/api/sessions/{sid}").json()
    turns = client.get(f"/api/sessions/{sid}/turns").json()
    assert len(session["messages"]) == 2
    assert len(turns) == 2
    assert turns[0]["content"] == "Tell me about your running."


def test_turns_endpoint_exposes_no_protected_content(client):
    sid = _session(client, case_id="referral_case_01")
    client.post(
        f"/api/interviews/{sid}/messages",
        json={"text": "How is your knee?", "caseId": "referral_case_01", "clientTurnId": "p-1"},
    )
    import json as _json
    text = _json.dumps(client.get(f"/api/sessions/{sid}/turns").json()).lower()
    for marker in ("hidden_context", "referral_context", "disclosure_guidance",
                   "interprofessional", "care_pathways"):
        assert marker not in text
