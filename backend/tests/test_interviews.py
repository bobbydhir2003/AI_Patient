import pytest


@pytest.fixture()
def client(student_client):
    return student_client


@pytest.fixture()
def failing_client(failing_student_client):
    return failing_student_client


def _start(client, case_id="camden"):
    response = client.post(
        "/api/sessions",
        json={"studentName": "Interview Tester", "studentId": "", "caseId": case_id},
    )
    return response.json()["sessionId"]


def _send(client, session_id, text, case_id="camden"):
    return client.post(
        f"/api/interviews/{session_id}/messages",
        json={"text": text, "caseId": case_id},
    )


def test_exchange_persists_transcript(client):
    session_id = _start(client)
    r1 = _send(client, session_id, "Hi Camden, how are you today?")
    assert r1.status_code == 200
    body = r1.json()
    assert body["status"] == "completed"
    assert body["patientText"].strip()
    assert body["turnId"]
    # internal fields must NOT be exposed to the frontend
    assert "usedFactIds" not in body and "used_fact_ids" not in body

    r2 = _send(client, session_id, "What do you like to play?")
    assert r2.status_code == 200

    transcript = client.get(f"/api/sessions/{session_id}").json()["messages"]
    assert len(transcript) == 4
    assert [m["sender"] for m in transcript] == ["student", "patient", "student", "patient"]
    assert transcript[0]["text"] == "Hi Camden, how are you today?"


def test_openai_failure_returns_error_and_saves_nothing(failing_client):
    session_id = _start(failing_client)
    response = _send(failing_client, session_id, "Hello?")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PATIENT_RESPONSE_UNAVAILABLE"
    # No fake turn was persisted - the transcript is untouched.
    transcript = failing_client.get(f"/api/sessions/{session_id}").json()["messages"]
    assert transcript == []


def test_case_session_mismatch_rejected(client):
    session_id = _start(client, case_id="camden")
    response = _send(client, session_id, "Hi", case_id="carly")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "case_session_mismatch"
    transcript = client.get(f"/api/sessions/{session_id}").json()["messages"]
    assert transcript == []


def test_empty_message_rejected(client):
    session_id = _start(client)
    response = _send(client, session_id, "")
    assert response.status_code == 422


def test_message_to_missing_session(client):
    response = _send(client, "nope", "Hello")
    assert response.status_code == 404


def test_patient_turn_stores_generation_metadata(client, engine):
    from sqlalchemy import text as sql_text

    session_id = _start(client, case_id="jayden")
    _send(client, session_id, "Tell me about your running program.", case_id="jayden")
    with engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT role, model_name, prompt_version, validation_status FROM conversation_turns ORDER BY turn_index"
            )
        ).all()
    assert len(rows) == 2
    student, patient = rows
    assert student.model_name is None
    assert patient.model_name  # model recorded
    assert patient.prompt_version
    assert patient.validation_status == "valid"


def test_transcript_survives_completion(client):
    session_id = _start(client, case_id="jayden")
    _send(client, session_id, "Tell me about your running program.", case_id="jayden")
    done = client.post(f"/api/sessions/{session_id}/complete").json()
    assert done["locked"] is True
    assert len(done["messages"]) == 2
