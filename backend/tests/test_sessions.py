def _create(client, case_id="camden", name="Test Student", student_id="12345"):
    return client.post(
        "/api/sessions",
        json={"studentName": name, "studentId": student_id, "caseId": case_id},
    )


def test_create_session(client):
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["caseId"] == "camden"
    assert body["status"] == "active"
    assert body["locked"] is False
    assert body["studentName"] == "Test Student"
    assert body["messages"] == []
    assert body["sessionId"]


def test_create_session_unknown_case(client):
    assert _create(client, case_id="unknown").status_code == 404


def test_get_session_roundtrip(client):
    session_id = _create(client).json()["sessionId"]
    response = client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json()["sessionId"] == session_id


def test_get_missing_session(client):
    assert client.get("/api/sessions/doesnotexist").status_code == 404


def _seed_exchange(client, session_id, case_id="camden"):
    client.post(
        f"/api/interviews/{session_id}/messages",
        json={"text": "Hi, how are you today?", "caseId": case_id},
    )


def test_complete_locks_session(client):
    session_id = _create(client).json()["sessionId"]
    _seed_exchange(client, session_id)
    done = client.post(f"/api/sessions/{session_id}/complete")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "completed"
    assert body["locked"] is True
    assert body["completedAt"] is not None
    again = client.post(f"/api/sessions/{session_id}/complete")
    assert again.status_code == 200
    assert again.json()["locked"] is True


def test_locked_session_rejects_messages(client):
    session_id = _create(client).json()["sessionId"]
    _seed_exchange(client, session_id)
    client.post(f"/api/sessions/{session_id}/complete")
    response = client.post(
        f"/api/interviews/{session_id}/messages", json={"text": "Hello?", "caseId": "camden"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_locked"
