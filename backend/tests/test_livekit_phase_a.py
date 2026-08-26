"""Tests for Phase A: the VOICE_ENGINE feature flag and the student-safe
LiveKit token endpoint (POST /api/interviews/{session_id}/livekit-token).

Nothing here touches the real InterviewPage or changes any student-facing
behavior - no frontend code was modified in Phase A, and this endpoint is not
called by anything yet. These tests exist to prove the flag/endpoint are
correct and safely inert by default, ready for a later phase to actually
wire a UI to them.

Real JWT signing (livekit-api), no network calls - same discipline as
test_livekit_poc.py.
"""
import json

import jwt
import pytest

from app.core.config import Settings, get_settings
from app.patient_engine.case_loader import load_case
from app.services import livekit_token_service
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, make_admin, register

LIVEKIT_URL = "wss://fake-project.livekit.cloud"
LIVEKIT_API_KEY = "test-lk-key"
LIVEKIT_API_SECRET = "test-lk-secret-at-least-32-chars-long"


def _enable_livekit_for_students(monkeypatch, *, voice_engine="livekit"):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", True)
    monkeypatch.setattr(settings, "livekit_url", LIVEKIT_URL)
    monkeypatch.setattr(settings, "livekit_api_key", LIVEKIT_API_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", LIVEKIT_API_SECRET)
    monkeypatch.setattr(settings, "voice_engine", voice_engine)


def _student_client(engine, *, email="lkstudent@school.edu", number="LKS1"):
    client = make_client(engine, authenticate=False)
    register(client, email=email, password="studpass1", number=number)
    token = login_token(client, email, "studpass1")
    client.headers.update(auth_header(token))
    return client


def _owned_session_id(client, case_id="carly") -> str:
    resp = client.post(
        "/api/sessions", json={"studentName": "S", "studentId": "1", "caseId": case_id}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["sessionId"]


# ------------------------------------------------------------- VOICE_ENGINE flag
def test_voice_engine_defaults_to_legacy():
    """Default MUST remain legacy - no env var, no override."""
    assert Settings(_env_file=None).voice_engine == "legacy"


def test_invalid_voice_engine_falls_back_to_legacy_safely():
    """An unrecognized VOICE_ENGINE value must never crash the app and must
    never silently activate the unvalidated engine - it fails safe to the
    known-good default."""
    assert Settings(_env_file=None, voice_engine="not-a-real-engine").voice_engine == "legacy"
    assert Settings(_env_file=None, voice_engine="").voice_engine == "legacy"
    assert Settings(_env_file=None, voice_engine="LIVEKIT").voice_engine == "livekit"  # case-insensitive, valid


def test_voice_engine_livekit_is_accepted_verbatim():
    assert Settings(_env_file=None, voice_engine="livekit").voice_engine == "livekit"


# ------------------------------------------------- student-safe token endpoint
def test_student_token_disabled_by_default_even_with_livekit_cloud_configured(engine, monkeypatch):
    """TEST (item 2/7): even with real-looking LiveKit Cloud credentials and
    LIVEKIT_POC_ENABLED=true, the student-safe endpoint stays closed unless
    VOICE_ENGINE is ALSO explicitly "livekit" - the flag has real teeth, not
    just documentation value. Default production config (VOICE_ENGINE=legacy)
    must never let a student obtain a LiveKit token."""
    _enable_livekit_for_students(monkeypatch, voice_engine="legacy")  # the actual production default
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    r = client.post(f"/api/interviews/{session_id}/livekit-token")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "livekit_not_configured"


def test_student_can_get_livekit_token_for_own_session(engine, monkeypatch):
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    r = client.post(f"/api/interviews/{session_id}/livekit-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"token", "url", "roomName", "participantIdentity"}
    assert body["url"] == LIVEKIT_URL
    assert body["roomName"] == f"ptai-interview-{session_id}"
    assert isinstance(body["token"], str) and body["token"].count(".") == 2  # JWT shape


def test_student_denied_token_for_another_students_session(engine, monkeypatch):
    """TEST (item 4/7): no ability to obtain a token for another student's
    session - same non-leaking 404 require_session_access already gives
    every other session-scoped endpoint."""
    _enable_livekit_for_students(monkeypatch)
    owner_client = _student_client(engine, email="owner@school.edu", number="OWN1")
    owner_session_id = _owned_session_id(owner_client)

    other_client = _student_client(engine, email="intruder@school.edu", number="INT1")
    r = other_client.post(f"/api/interviews/{owner_session_id}/livekit-token")
    assert r.status_code == 404


def test_token_denied_for_nonexistent_session(engine, monkeypatch):
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    r = client.post("/api/interviews/does-not-exist/livekit-token")
    assert r.status_code == 404


def test_token_requires_authentication(engine, monkeypatch):
    _enable_livekit_for_students(monkeypatch)
    client = make_client(engine, authenticate=False)
    r = client.post("/api/interviews/some-session/livekit-token")
    assert r.status_code == 401


def test_admin_can_also_reach_the_student_endpoint_for_any_session(engine, monkeypatch):
    """user_can_access_session's existing rule (admins may access any
    session) applies here too, via the SAME require_session_access
    dependency - no separate admin carve-out was written."""
    _enable_livekit_for_students(monkeypatch)
    student_client = _student_client(engine)
    session_id = _owned_session_id(student_client)

    admin_client = make_client(engine, authenticate=False)
    make_admin(engine, email="lkadmin-a@school.edu", password="adminpass1")
    admin_token = login_token(admin_client, "lkadmin-a@school.edu", "adminpass1")
    admin_client.headers.update(auth_header(admin_token))

    r = admin_client.post(f"/api/interviews/{session_id}/livekit-token")
    assert r.status_code == 200, r.text


def test_room_session_and_case_metadata_are_correct_and_server_derived(engine, monkeypatch):
    """TEST (item: room/session/case metadata correct): decode the real
    signed JWT and verify the dispatch metadata matches the verified session
    exactly - never a client-influenced value (the request body has no such
    field at all)."""
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client, case_id="carly")

    r = client.post(f"/api/interviews/{session_id}/livekit-token")
    assert r.status_code == 200, r.text
    decoded = jwt.decode(
        r.json()["token"], LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert decoded["video"]["room"] == f"ptai-interview-{session_id}"
    assert decoded["video"]["roomJoin"] is True

    agents = decoded["roomConfig"]["agents"]
    assert len(agents) == 1
    assert agents[0]["agentName"] == get_settings().livekit_agent_name == "ptai-patient-agent"
    metadata = json.loads(agents[0]["metadata"])
    assert metadata == {"session_id": session_id, "case_id": "carly"}


def test_student_token_response_never_contains_api_secret(engine, monkeypatch):
    _enable_livekit_for_students(monkeypatch)
    client = _student_client(engine)
    session_id = _owned_session_id(client)

    r = client.post(f"/api/interviews/{session_id}/livekit-token")
    assert r.status_code == 200
    body = r.json()
    serialized = r.text
    assert LIVEKIT_API_SECRET not in serialized

    decoded = jwt.decode(
        body["token"], LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert LIVEKIT_API_SECRET not in str(decoded)
    assert decoded["iss"] == LIVEKIT_API_KEY


def test_case_session_consistency_rejects_a_session_with_an_invalid_case(engine, monkeypatch, db_session):
    """TEST (item 4: case/session consistency): a session row whose case_id
    no longer resolves to a real case must never get a token minted for it -
    the LiveKit agent could never generate a patient response for it anyway."""
    _enable_livekit_for_students(monkeypatch)
    from app.core.security import hash_password
    from app.models import InterviewSession, Student, User

    student = Student(name="Bad Case Student", student_number="BC1", email="badcase@school.edu")
    db_session.add(student)
    db_session.flush()
    user = User(
        email="badcase@school.edu", password_hash=hash_password("x"), full_name="Bad Case",
        role="student", student_id=student.id, is_active=True,
    )
    db_session.add(user)
    # Bypasses session_service.create_session (which validates the case via
    # load_case at creation time) to simulate a stale/corrupted row.
    session = InterviewSession(student_id=student.id, case_id="not-a-real-case", case_category="standard")
    db_session.add(session)
    db_session.commit()

    client = make_client(engine, authenticate=False)
    resp = client.post("/api/auth/login", json={"email": "badcase@school.edu", "password": "x"})
    assert resp.status_code == 200, resp.text
    client.headers.update(auth_header(resp.json()["accessToken"]))

    r = client.post(f"/api/interviews/{session.id}/livekit-token")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "case_not_found"


# ------------------------------------------------- admin POC endpoint unaffected
def test_admin_poc_token_endpoint_still_works_and_uses_the_poc_room_prefix(engine, monkeypatch):
    """TEST (item 5/9): the existing admin LiveKit POC endpoint (Phase 1/2)
    is completely unaffected by Phase A's refactor of livekit_token_service -
    still works, and still mints ptai-poc- rooms, never the new
    ptai-interview- prefix."""
    _enable_livekit_for_students(monkeypatch)  # also sets livekit_poc_enabled etc.
    client = make_client(engine, authenticate=False)
    make_admin(engine, email="lkadmin-poc@school.edu", password="adminpass1")
    token = login_token(client, "lkadmin-poc@school.edu", "adminpass1")
    client.headers.update(auth_header(token))
    session_id = _owned_session_id(client)

    r = client.post("/api/livekit/token", json={"sessionId": session_id})
    assert r.status_code == 200, r.text
    assert r.json()["roomName"] == f"ptai-poc-{session_id}"
    assert not r.json()["roomName"].startswith("ptai-interview-")


def test_poc_and_student_room_names_use_distinct_prefixes():
    assert livekit_token_service.poc_room_name("abc") == "ptai-poc-abc"
    assert livekit_token_service.student_room_name("abc") == "ptai-interview-abc"
    assert livekit_token_service.poc_room_name("abc") != livekit_token_service.student_room_name("abc")


def test_student_livekit_enabled_requires_both_engine_flag_and_cloud_config(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_poc_enabled", True)
    monkeypatch.setattr(settings, "livekit_url", LIVEKIT_URL)
    monkeypatch.setattr(settings, "livekit_api_key", LIVEKIT_API_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", LIVEKIT_API_SECRET)

    monkeypatch.setattr(settings, "voice_engine", "legacy")
    assert livekit_token_service.student_livekit_enabled() is False

    monkeypatch.setattr(settings, "voice_engine", "livekit")
    assert livekit_token_service.student_livekit_enabled() is True

    monkeypatch.setattr(settings, "livekit_poc_enabled", False)
    assert livekit_token_service.student_livekit_enabled() is False


def test_carly_case_still_loads_via_case_loader_sanity_check():
    """Sanity check for the case/session consistency test above: "carly" is
    a real case, so the happy-path tests above are exercising a genuine
    load_case() success, not a coincidentally-never-called check."""
    case = load_case("carly")
    assert case.case_id == "carly"
