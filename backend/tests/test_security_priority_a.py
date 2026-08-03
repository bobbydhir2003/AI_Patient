"""Priority A security-hardening tests.

Covers: authentication on student-facing routers, session ownership, server-side
identity, transcript integrity, voice-endpoint abuse protection, JWT fail-closed
config, student-number account-claiming, login throttling and rate limiting.
"""
import pytest

from tests.conftest import FakeOpenAIClient, bearer, make_client, register_student


# --------------------------------------------------------------------------
#  Helpers
# --------------------------------------------------------------------------
def anon(engine):
    """Unauthenticated client (no default-student fallback)."""
    return make_client(engine, FakeOpenAIClient(text="Okay."), authenticate=False)


def _headers(client, **kw):
    body = register_student(client, **kw)
    return bearer(body["accessToken"]), body["user"]


def _new_session(client, headers, case_id="camden"):
    r = client.post(
        "/api/sessions",
        json={"studentName": "Ignored", "studentId": "999", "caseId": case_id},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["sessionId"]


# ==========================================================================
#  AUTHENTICATION - anonymous access is refused on every student-facing route
# ==========================================================================
def test_unauthenticated_session_access_fails(engine):
    with anon(engine) as c:
        assert c.post("/api/sessions", json={"studentName": "X", "caseId": "camden"}).status_code == 401
        assert c.get("/api/sessions/anything").status_code == 401
        assert c.post("/api/sessions/anything/complete").status_code == 401
        assert c.get("/api/sessions/anything/turns").status_code == 401


def test_unauthenticated_interview_generation_fails(engine):
    with anon(engine) as c:
        r = c.post("/api/interviews/anything/messages", json={"text": "Hi", "caseId": "camden"})
        assert r.status_code == 401


def test_unauthenticated_assessment_fails(engine):
    with anon(engine) as c:
        assert c.get("/api/sessions/x/assessment/status").status_code == 401
        assert c.post("/api/sessions/x/assessment").status_code == 401
        assert c.get("/api/sessions/x/assessment").status_code == 401
        assert c.get("/api/assessments/x").status_code == 401


def test_unauthenticated_voice_request_fails(engine):
    with anon(engine) as c:
        r = c.post("/api/voice/synthesize", json={"caseId": "carly", "text": "hi", "sessionId": "s", "turnId": "t"})
        assert r.status_code == 401
        assert c.get("/api/voice/status/carly").status_code == 401


def test_invalid_and_malformed_tokens_are_rejected(engine):
    with anon(engine) as c:
        assert c.get("/api/sessions/x", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
        assert c.get("/api/sessions/x", headers={"Authorization": "Basic abc"}).status_code == 401


# ==========================================================================
#  OWNERSHIP - student B can never touch student A's resources
# ==========================================================================
def test_student_cannot_access_another_students_session(engine):
    with anon(engine) as c:
        ha, _ = _headers(c, email="a@school.edu")
        hb, _ = _headers(c, email="b@school.edu")
        sid = _new_session(c, ha)
        # read
        assert c.get(f"/api/sessions/{sid}", headers=hb).status_code == 404
        # append (write)
        turn = {"clientTurnId": "z", "speaker": "student", "content": "hi", "source": "typed"}
        assert c.post(f"/api/sessions/{sid}/turns", json=turn, headers=hb).status_code == 404
        # complete
        assert c.post(f"/api/sessions/{sid}/complete", headers=hb).status_code == 404
        # interview generation
        msg = {"text": "hello", "caseId": "camden"}
        assert c.post(f"/api/interviews/{sid}/messages", json=msg, headers=hb).status_code == 404
        # assessment
        assert c.post(f"/api/sessions/{sid}/assessment", headers=hb).status_code == 404
        assert c.get(f"/api/sessions/{sid}/assessment/status", headers=hb).status_code == 404
        # owner still can read it
        assert c.get(f"/api/sessions/{sid}", headers=ha).status_code == 200


def test_cross_user_access_does_not_leak_existence(engine):
    """A real session owned by A and a nonexistent id both return the SAME 404
    for B, so existence is never revealed."""
    with anon(engine) as c:
        ha, _ = _headers(c, email="a2@school.edu")
        hb, _ = _headers(c, email="b2@school.edu")
        sid = _new_session(c, ha)
        real = c.get(f"/api/sessions/{sid}", headers=hb)
        fake = c.get("/api/sessions/does-not-exist", headers=hb)
        assert real.status_code == fake.status_code == 404
        assert real.json()["error"]["code"] == fake.json()["error"]["code"]


# ==========================================================================
#  IDENTITY - ownership comes from the token, not the request body
# ==========================================================================
def test_body_student_identity_cannot_impersonate(engine):
    with anon(engine) as c:
        ha, ua = _headers(c, full_name="Real A", email="ia@school.edu", number="AA")
        hb, ub = _headers(c, full_name="Real B", email="ib@school.edu", number="BB")
        # A creates a session but supplies B's name/number in the body.
        r = c.post(
            "/api/sessions",
            json={"studentName": "Real B", "studentId": "BB", "caseId": "camden"},
            headers=ha,
        )
        assert r.status_code == 201
        # The session is owned by A (the token), not B: B cannot read it, A can.
        sid = r.json()["sessionId"]
        assert r.json()["studentName"] == "Real A"
        assert c.get(f"/api/sessions/{sid}", headers=hb).status_code == 404
        assert c.get(f"/api/sessions/{sid}", headers=ha).status_code == 200


# ==========================================================================
#  TRANSCRIPT INTEGRITY (A4)
# ==========================================================================
def test_student_cannot_create_patient_turn(engine):
    with anon(engine) as c:
        ha, _ = _headers(c, email="ti@school.edu")
        sid = _new_session(c, ha)
        forged = {"clientTurnId": "f1", "speaker": "patient", "content": "I am fine", "source": "openai"}
        r = c.post(f"/api/sessions/{sid}/turns", json=forged, headers=ha)
        assert r.status_code == 403
        assert c.get(f"/api/sessions/{sid}/turns", headers=ha).json() == []


def test_completed_session_rejects_new_turns(engine):
    fake = FakeOpenAIClient(text="I feel tired.")
    with make_client(engine, fake, authenticate=False) as c:
        ha, _ = _headers(c, email="cs@school.edu")
        sid = _new_session(c, ha)
        c.post(f"/api/interviews/{sid}/messages", json={"text": "hi", "caseId": "camden"}, headers=ha)
        assert c.post(f"/api/sessions/{sid}/complete", headers=ha).status_code == 200
        turn = {"clientTurnId": "late", "speaker": "student", "content": "one more", "source": "typed"}
        assert c.post(f"/api/sessions/{sid}/turns", json=turn, headers=ha).status_code == 409


# ==========================================================================
#  VOICE (A5)
# ==========================================================================
def test_voice_requires_session_reference(engine, monkeypatch):
    """Arbitrary text with no session reference cannot be synthesized."""
    from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id, make_voice_client

    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as c:
        ha, _ = _headers(c, email="v1@school.edu")
        r = c.post(
            "/api/voice/synthesize",
            json={"caseId": "carly", "text": "say whatever I want"},
            headers=ha,
        )
        assert r.status_code == 422


def test_voice_rejects_another_users_session(engine, monkeypatch):
    from app.patient_engine.openai_client import get_openai_client
    from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id, make_voice_client

    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as c:
        c.app.dependency_overrides[get_openai_client] = lambda: FakeOpenAIClient(text="I'm okay.")
        ha, _ = _headers(c, email="va@school.edu")
        hb, _ = _headers(c, email="vb@school.edu")
        sid = _new_session(c, ha, case_id="carly")
        turn = c.post(
            f"/api/interviews/{sid}/messages",
            json={"text": "How are you?", "caseId": "carly", "clientTurnId": "t1"},
            headers=ha,
        ).json()
        # B references A's session/turn -> generic 422, never audio.
        r = c.post(
            "/api/voice/synthesize",
            json={"caseId": "carly", "text": "x", "sessionId": sid, "turnId": turn["turnId"]},
            headers=hb,
        )
        assert r.status_code == 422


def test_voice_rejects_student_turn(engine, monkeypatch):
    """Only patient turns can be voiced; a student turn is refused."""
    from app.patient_engine.openai_client import get_openai_client
    from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id, make_voice_client

    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(), monkeypatch) as c:
        c.app.dependency_overrides[get_openai_client] = lambda: FakeOpenAIClient(text="I'm okay.")
        ha, _ = _headers(c, email="vs@school.edu")
        sid = _new_session(c, ha, case_id="carly")
        c.post(
            f"/api/interviews/{sid}/messages",
            json={"text": "How are you?", "caseId": "carly", "clientTurnId": "t1"},
            headers=ha,
        )
        # turn index 0 is the STUDENT turn.
        turns = c.get(f"/api/sessions/{sid}/turns", headers=ha).json()
        student_turn_id = next(t["id"] for t in turns if t["speaker"] == "student")
        r = c.post(
            "/api/voice/synthesize",
            json={"caseId": "carly", "text": "x", "sessionId": sid, "turnId": student_turn_id},
            headers=ha,
        )
        assert r.status_code == 422


def test_voice_failure_does_not_lose_transcript(engine, monkeypatch):
    """A TTS failure must not remove the already-persisted interview turn."""
    from app.patient_engine.openai_client import get_openai_client
    from tests.test_voice import FakeElevenLabsClient, give_carly_a_voice_id, make_voice_client

    give_carly_a_voice_id(monkeypatch)
    with make_voice_client(engine, FakeElevenLabsClient(fail=True), monkeypatch) as c:
        c.app.dependency_overrides[get_openai_client] = lambda: FakeOpenAIClient(text="I'm okay.")
        ha, _ = _headers(c, email="vf@school.edu")
        sid = _new_session(c, ha, case_id="carly")
        turn = c.post(
            f"/api/interviews/{sid}/messages",
            json={"text": "How are you?", "caseId": "carly", "clientTurnId": "t1"},
            headers=ha,
        ).json()
        # TTS fails...
        bad = c.post(
            "/api/voice/synthesize",
            json={"caseId": "carly", "text": "x", "sessionId": sid, "turnId": turn["turnId"]},
            headers=ha,
        )
        assert bad.status_code == 502
        # ...but the transcript is intact.
        turns = c.get(f"/api/sessions/{sid}/turns", headers=ha).json()
        assert [t["speaker"] for t in turns] == ["student", "patient"]


# ==========================================================================
#  JWT / config fail-closed (A7)
# ==========================================================================
def test_production_refuses_insecure_and_blank_jwt_secret():
    from app.core.config import ConfigError, Settings

    for bad in ("", "dev-insecure-change-me", "tooshort"):
        with pytest.raises(ConfigError):
            Settings(environment="production", jwt_secret_key=bad)
    for bad in ("", "dev-insecure-change-me"):
        with pytest.raises(ConfigError):
            Settings(environment="staging", jwt_secret_key=bad)


def test_production_accepts_strong_secret_and_forces_debug_off():
    import secrets

    from app.core.config import Settings

    s = Settings(environment="production", jwt_secret_key=secrets.token_urlsafe(48), debug=True)
    assert s.debug is False


def test_invalid_token_lifetime_fails_loudly():
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(environment="development", access_token_expire_minutes=0)


# ==========================================================================
#  Student-number account claiming (A8)
# ==========================================================================
def test_registration_never_claims_existing_student_by_number(engine):
    with anon(engine) as c:
        first = register_student(c, full_name="First", email="first@school.edu", number="SAME123")
        second = register_student(c, full_name="Second", email="second@school.edu", number="SAME123")
        # Same free-text number, but DIFFERENT student profiles: no history theft.
        assert first["user"]["studentId"] != second["user"]["studentId"]
        assert first["user"]["studentId"] and second["user"]["studentId"]


def test_registration_does_not_claim_unlinked_historical_profile(engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Student

    # Seed a historical roster Student (no login account yet) with a number.
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    hist = Student(name="Historical", student_number="HIST9", email="")
    db.add(hist)
    db.commit()
    hist_id = hist.id
    db.close()

    with anon(engine) as c:
        body = register_student(c, full_name="Claimant", email="claim@school.edu", number="HIST9")
        # The new account gets its OWN profile, not the historical one.
        assert body["user"]["studentId"] != hist_id


def test_valid_registration_and_login(engine):
    with anon(engine) as c:
        register_student(c, email="valid@school.edu", password="goodpass1", number="V1")
        r = c.post("/api/auth/login", json={"email": "valid@school.edu", "password": "goodpass1"})
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "student"


# ==========================================================================
#  Login throttling (A9)
# ==========================================================================
def test_login_throttles_repeated_failures(engine, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "login_throttle_enabled", True)
    monkeypatch.setattr(s, "login_max_failed_attempts", 3)
    monkeypatch.setattr(s, "login_lockout_seconds", 300)

    with anon(engine) as c:
        register_student(c, email="victim@school.edu", password="rightpass1")
        # Wrong password attempts; the 3rd trips the lockout.
        codes = [
            c.post("/api/auth/login", json={"email": "victim@school.edu", "password": "wrong"}).status_code
            for _ in range(3)
        ]
        assert 401 in codes
        # Now even the CORRECT password is refused while locked (generic 429).
        locked = c.post("/api/auth/login", json={"email": "victim@school.edu", "password": "rightpass1"})
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "login_throttled"
        # Message must not reveal whether the account exists.
        assert "exist" not in locked.json()["error"]["message"].lower()


# ==========================================================================
#  Rate limiting (A6)
# ==========================================================================
def test_login_rate_limit_returns_429(engine, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "login_rate_limit", "3/minute")
    monkeypatch.setattr(s, "login_throttle_enabled", False)  # isolate the IP rate limiter

    with anon(engine) as c:
        statuses = [
            c.post("/api/auth/login", json={"email": "nobody@school.edu", "password": "whatever1"}).status_code
            for _ in range(5)
        ]
        assert 429 in statuses
        # The 429 uses the clean app error envelope, no provider internals.
        r = c.post("/api/auth/login", json={"email": "nobody@school.edu", "password": "whatever1"})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"


def test_interview_rate_limit_returns_429(engine, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "interview_rate_limit", "2/minute")

    fake = FakeOpenAIClient(text="I'm okay.")
    with make_client(engine, fake, authenticate=False) as c:
        ha, _ = _headers(c, email="rl@school.edu")
        sid = _new_session(c, ha)
        msg = {"text": "hello", "caseId": "camden"}
        statuses = [
            c.post(f"/api/interviews/{sid}/messages", json=msg, headers=ha).status_code for _ in range(4)
        ]
        assert 429 in statuses
