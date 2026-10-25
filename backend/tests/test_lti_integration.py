"""AI iEXCEL Course 1 integration (LTI 1.3 tool): launch validation, identity
mapping, course isolation, case assignment, instructor scope, and progress
reporting. The platform is simulated with a locally generated RSA key; no
network, no production configuration, synthetic users only."""
import json
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import ConfigError, Settings, get_settings
from app.core.security import create_access_token
from app.models import InterviewSession, PlatformEventOutbox, PlatformIdentity, User
from app.services import lti_service, platform_events
from tests.conftest import FakeOpenAIClient, bearer, make_client, seed_exchange

ISSUER = "https://platform.test"
CLIENT_ID = "aisp-client-1"
DEPLOYMENT = "dep-course1"
TOOL = "https://aisp.test"
CTX_A = str(uuid.UUID(int=0xA))
CTX_B = str(uuid.UUID(int=0xB))
LTI = "https://purl.imsglobal.org/spec/lti/claim/"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PLATFORM_KEY = _rsa()
OTHER_KEY = _rsa()
TOOL_KEY = _rsa()


def _jwk(key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


class _JwksResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"keys": [_jwk(PLATFORM_KEY, "platform-1")]}


@pytest.fixture()
def lti_env(monkeypatch, tmp_path):
    key_file = tmp_path / "tool.pem"
    key_file.write_bytes(
        TOOL_KEY.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    env = {
        "AI_SP_LTI_ENABLED": "true",
        "LTI_PLATFORM_ISSUER": ISSUER,
        "LTI_CLIENT_ID": CLIENT_ID,
        "LTI_DEPLOYMENT_IDS": DEPLOYMENT,
        "LTI_AUTHORIZATION_URL": f"{ISSUER}/api/v1/lti/authorize",
        "LTI_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
        "LTI_TOKEN_URL": f"{ISSUER}/api/v1/lti/token",
        "LTI_EVENTS_URL": f"{ISSUER}/api/v1/integration/events",
        "LTI_LAUNCH_URL": f"{TOOL}/api/lti/launch",
        "LTI_FRONTEND_URL": TOOL,
        "LTI_TOOL_KEY_FILE": str(key_file),
        "LTI_TOOL_KEY_ID": "tool-1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lti_service.httpx, "get", lambda url, timeout=None: _JwksResponse())
    get_settings.cache_clear()
    lti_service.platform_keys.clear()
    yield env
    get_settings.cache_clear()
    lti_service.platform_keys.clear()


@pytest.fixture()
def api(engine, lti_env):
    fake = FakeOpenAIClient(text="I get tired fast.")
    with make_client(engine, fake, authenticate=False) as client:
        client.fake = fake
        yield client


def _claims(nonce, *, sub="platform-sub-1", role=LEARNER, ctx=CTX_A, **over):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        LTI + "message_type": "LtiResourceLinkRequest",
        LTI + "version": "1.3.0",
        LTI + "deployment_id": DEPLOYMENT,
        LTI + "target_link_uri": f"{TOOL}/api/lti/launch",
        LTI + "resource_link": {"id": "rl-1", "title": "AI Standardized Patient"},
        LTI + "roles": [role],
        LTI + "context": {"id": ctx, "label": "PT-AISP", "title": "AI Standardized Patient Dev"},
        LTI + "custom": {"iexcel_contract_version": "iexcel-integration/1"},
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _login(client, **over):
    params = {
        "iss": ISSUER,
        "login_hint": "hint-1",
        "target_link_uri": f"{TOOL}/api/lti/launch",
        "lti_message_hint": str(uuid.uuid4()),
        "client_id": CLIENT_ID,
        "lti_deployment_id": DEPLOYMENT,
    }
    params.update(over)
    return client.get("/api/lti/login", params=params, follow_redirects=False)


def _start(client):
    r = _login(client)
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["redirect_uri"] == [f"{TOOL}/api/lti/launch"]
    assert "aisp_lti_state=" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    return query["state"][0], query["nonce"][0]


def _post_launch(client, state, id_token, cookie_state=None):
    return client.post(
        "/api/lti/launch",
        content=urlencode({"state": state, "id_token": id_token}),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"aisp_lti_state={state if cookie_state is None else cookie_state}",
        },
        follow_redirects=False,
    )


def _sign(claims, key=PLATFORM_KEY, kid="platform-1"):
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _launch_code(client, **claim_over):
    state, nonce = _start(client)
    r = _post_launch(client, state, _sign(_claims(nonce, **claim_over)))
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(f"{TOOL}/lti/callback#code="), location
    assert "?" not in location  # nothing in a query string
    return location.split("#code=", 1)[1]


def _launch(client, **claim_over):
    code = _launch_code(client, **claim_over)
    r = client.post("/api/lti/session/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _assign(client, instructor_token, cases):
    r = client.put("/api/lti/instructor/cases", json={"caseIds": cases}, headers=bearer(instructor_token))
    assert r.status_code == 200, r.text
    return r.json()


def _db(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


# ------------------------------------------------------------------ feature flag


def test_integration_is_off_by_default(engine, monkeypatch):
    monkeypatch.delenv("AI_SP_LTI_ENABLED", raising=False)
    get_settings.cache_clear()
    with make_client(engine, authenticate=False) as client:
        assert client.get("/api/lti/login").status_code == 404
        assert client.post("/api/lti/launch").status_code == 404
        assert client.post("/api/lti/session/exchange", json={"code": "x"}).status_code == 404
        # A course-scoped token is refused outright while the flag is off.
        from tests.conftest import seed_default_student

        uid = seed_default_student(engine)
        token = create_access_token(subject=uid, role="student", lti={"ctx": CTX_A, "role": "learner"})
        assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401
    get_settings.cache_clear()


def test_dev_fake_ai_refused_outside_development():
    with pytest.raises(ConfigError):
        Settings(environment="production", jwt_secret_key="x" * 48, ai_sp_dev_fake_ai=True)


# -------------------------------------------------------------- launch validation


def test_valid_learner_launch_creates_course_session(api):
    body = _launch(api, name="Synthetic Learner")
    assert body["session"]["role"] == "learner"
    assert body["session"]["context"] == {
        "id": CTX_A, "label": "PT-AISP", "title": "AI Standardized Patient Dev",
    }
    assert body["session"]["user"]["fullName"] == "Synthetic Learner"
    assert body["session"]["user"]["role"] == "student"
    assert body["expiresIn"] == 3600
    payload = jwt.decode(body["accessToken"], options={"verify_signature": False})
    assert payload["lti"] == {"ctx": CTX_A, "role": "learner"}
    me = api.get("/api/lti/session", headers=bearer(body["accessToken"]))
    assert me.status_code == 200 and me.json()["context"]["id"] == CTX_A


@pytest.mark.parametrize(
    "over, key, reason",
    [
        ({}, OTHER_KEY, "invalid_id_token"),  # bad signature
        ({"iss": "https://evil.test"}, None, "wrong_issuer"),
        ({"aud": "someone-else", "azp": None}, None, "wrong_audience"),
        ({"azp": "someone-else"}, None, "wrong_audience"),
        ({LTI + "deployment_id": "dep-unknown"}, None, "unknown_deployment"),
        ({"exp": int(time.time()) - 60, "iat": int(time.time()) - 400}, None, "id_token_expired"),
        ({"nonce": "not-the-nonce"}, None, "nonce_mismatch"),
        ({LTI + "context": None}, None, "missing_context"),
        ({LTI + "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Mentor"]}, None, "unsupported_role"),
        ({LTI + "custom": {"iexcel_contract_version": "iexcel-integration/0"}}, None, "unsupported_contract_version"),
        ({LTI + "message_type": "LtiDeepLinkingRequest"}, None, "unsupported_message_type"),
        ({LTI + "version": "1.1"}, None, "unsupported_lti_version"),
        ({LTI + "resource_link": None}, None, "missing_resource_link"),
    ],
)
def test_launch_rejections(api, over, key, reason):
    state, nonce = _start(api)
    claims = _claims(nonce)
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    r = _post_launch(api, state, _sign(claims, key=key or PLATFORM_KEY))
    assert r.status_code == 303
    assert r.headers["location"] == f"{TOOL}/lti/error?reason={reason}"
    with _db(api._test_engine) as db:
        assert db.execute(select(func.count()).select_from(PlatformIdentity)).scalar_one() == 0


def test_unknown_signing_key_is_rejected(api):
    state, nonce = _start(api)
    r = _post_launch(api, state, _sign(_claims(nonce), key=OTHER_KEY, kid="forged"))
    assert r.headers["location"].endswith("reason=unknown_signing_key")


def test_state_must_match_cookie_and_is_single_use(api):
    state, nonce = _start(api)
    token = _sign(_claims(nonce))
    r = _post_launch(api, state, token, cookie_state="another-browser")
    assert r.headers["location"].endswith("reason=state_mismatch")
    assert _post_launch(api, state, token).status_code == 303  # first real use
    replay = _post_launch(api, state, token)
    assert replay.headers["location"].endswith("reason=state_replayed")


def test_login_rejects_unknown_platform_and_foreign_target(api):
    assert _login(api, iss="https://evil.test").headers["location"].endswith("reason=unknown_platform")
    assert _login(api, client_id="x").headers["location"].endswith("reason=unknown_client")
    r = _login(api, target_link_uri="https://evil.test/steal")
    assert r.headers["location"].endswith("reason=foreign_target_link")
    r = _login(api, lti_deployment_id="dep-unknown")
    assert r.headers["location"].endswith("reason=unknown_deployment")


def test_launch_code_is_single_use_and_short_lived(api):
    code = _launch_code(api)
    assert api.post("/api/lti/session/exchange", json={"code": code}).status_code == 200
    again = api.post("/api/lti/session/exchange", json={"code": code})
    assert again.status_code == 400 and again.json()["error"]["code"] == "lti_launch_rejected"
    code2 = _launch_code(api)
    with _db(api._test_engine) as db:
        from app.models import LtiLoginCode

        for row in db.execute(select(LtiLoginCode)).scalars():
            row.expires_at = row.expires_at.replace(year=2000)
        db.commit()
    assert api.post("/api/lti/session/exchange", json={"code": code2}).status_code == 400
    assert api.post("/api/lti/session/exchange", json={"code": "guess"}).status_code == 400


# ---------------------------------------------------------------- identity mapping


def test_repeated_launches_map_to_one_account(api):
    first = _launch(api)
    second = _launch(api)
    assert first["session"]["user"]["id"] == second["session"]["user"]["id"]
    with _db(api._test_engine) as db:
        assert db.execute(select(func.count()).select_from(PlatformIdentity)).scalar_one() == 1
    other = _launch(api, sub="platform-sub-2")
    assert other["session"]["user"]["id"] != first["session"]["user"]["id"]


def test_launch_never_links_an_existing_account_by_email_or_name(api):
    from tests.conftest import seed_default_student

    legacy_id = seed_default_student(
        api._test_engine, name="Pat Example", number="NUID-1", email="pat@school.test"
    )
    body = _launch(api, name="Pat Example", email="pat@school.test")
    assert body["session"]["user"]["id"] != legacy_id
    assert body["session"]["user"]["email"].endswith("@lti.invalid")
    with _db(api._test_engine) as db:
        legacy = db.get(User, legacy_id)
        assert (legacy.email, legacy.full_name, legacy.student_number) == (
            "pat@school.test", "Pat Example", "NUID-1",
        )
        assert db.execute(
            select(PlatformIdentity).where(PlatformIdentity.user_id == legacy_id)
        ).first() is None
    # The launched account cannot be used for direct password login.
    login = api.post("/api/auth/login", json={"email": body["session"]["user"]["email"], "password": "!"})
    assert login.status_code == 401


def test_direct_login_is_unchanged(engine, lti_env):
    from tests.conftest import register_student

    with make_client(engine, authenticate=False) as client:
        token = register_student(client)["accessToken"]
        r = client.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "sofia"},
                        headers=bearer(token))
        assert r.status_code == 201  # every case, as before
        with _db(engine) as db:
            assert db.get(InterviewSession, r.json()["sessionId"]).context_id is None
        assert client.get("/api/lti/session", headers=bearer(token)).status_code == 403


# ------------------------------------------------------ cases, isolation, instructor


def test_learner_sees_and_starts_only_assigned_cases(api):
    instructor = _launch(api, sub="inst-1", role=INSTRUCTOR)["accessToken"]
    learner = _launch(api)["accessToken"]
    assert api.get("/api/lti/cases", headers=bearer(learner)).json() == []
    out = _assign(api, instructor, ["camden", "carly"])
    assert out["assignedCaseIds"] == ["camden", "carly"]
    assert {c["id"] for c in out["assignableCases"]} == {"camden", "carly", "sofia", "jayden"}
    cases = api.get("/api/lti/cases", headers=bearer(learner)).json()
    assert [c["id"] for c in cases] == ["camden", "carly"]
    blocked = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "sofia"},
                       headers=bearer(learner))
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "case_not_assigned"
    ok = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                  headers=bearer(learner))
    assert ok.status_code == 201
    with _db(api._test_engine) as db:
        assert db.get(InterviewSession, ok.json()["sessionId"]).context_id == CTX_A
    bad = api.put("/api/lti/instructor/cases", json={"caseIds": ["referral_case_01"]},
                  headers=bearer(instructor))
    assert bad.status_code == 422


def test_course_isolation_between_offerings_and_learners(api):
    inst_a = _launch(api, sub="inst-a", role=INSTRUCTOR)["accessToken"]
    inst_b = _launch(api, sub="inst-b", role=INSTRUCTOR, ctx=CTX_B)["accessToken"]
    _assign(api, inst_a, ["camden"])
    _assign(api, inst_b, ["camden"])
    learner_a = _launch(api, sub="learner-1")["accessToken"]
    sid = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                   headers=bearer(learner_a)).json()["sessionId"]
    # Same platform user launched into offering B: same account, other course.
    learner_b = _launch(api, sub="learner-1", ctx=CTX_B)["accessToken"]
    assert api.get(f"/api/sessions/{sid}", headers=bearer(learner_b)).status_code == 404
    assert api.get("/api/students/me/sessions", headers=bearer(learner_b)).json() == []
    assert len(api.get("/api/students/me/sessions", headers=bearer(learner_a)).json()) == 1
    other_learner = _launch(api, sub="learner-2")["accessToken"]
    assert api.get(f"/api/sessions/{sid}", headers=bearer(other_learner)).status_code == 404
    # Instructors: own offering only.
    assert [s["sessionId"] for s in api.get("/api/lti/instructor/sessions", headers=bearer(inst_a)).json()] == [sid]
    assert api.get("/api/lti/instructor/sessions", headers=bearer(inst_b)).json() == []
    assert api.get(f"/api/lti/instructor/sessions/{sid}", headers=bearer(inst_b)).status_code == 404
    roster = api.get("/api/lti/instructor/roster", headers=bearer(inst_a)).json()
    assert sorted((m["sessionCount"], m["completedCount"]) for m in roster) == [(0, 0), (1, 0)]


def test_instructor_is_course_scoped_never_admin(api):
    inst = _launch(api, sub="inst-1", role=INSTRUCTOR)["accessToken"]
    learner = _launch(api)["accessToken"]
    _assign(api, inst, ["camden"])
    for path in ("/api/admin/dashboard", "/api/admin/students"):
        assert api.get(path, headers=bearer(inst)).status_code == 403
    assert api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                    headers=bearer(inst)).status_code == 403
    assert api.get("/api/students/me/sessions", headers=bearer(inst)).status_code == 403
    # Learners cannot reach instructor routes.
    for path in ("/api/lti/instructor/roster", "/api/lti/instructor/sessions", "/api/lti/instructor/cases"):
        assert api.get(path, headers=bearer(learner)).status_code == 403
    sid = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                   headers=bearer(learner)).json()["sessionId"]
    # Transcripts are off unless the faculty setting enables them.
    assert api.get(f"/api/lti/instructor/sessions/{sid}/transcript", headers=bearer(inst)).status_code == 403
    detail = api.get(f"/api/lti/instructor/sessions/{sid}", headers=bearer(inst)).json()
    assert detail["transcriptsEnabled"] is False and detail["assessment"] is None


# ------------------------------------------------------------- progress reporting


def _outbox(engine):
    with _db(engine) as db:
        return [
            (r.event_type, json.loads(r.payload))
            for r in db.execute(select(PlatformEventOutbox).order_by(PlatformEventOutbox.created_at)).scalars()
        ]


def _course_interview(api):
    inst = _launch(api, sub="inst-1", role=INSTRUCTOR)["accessToken"]
    _assign(api, inst, ["camden"])
    learner = _launch(api)["accessToken"]
    sid = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                   headers=bearer(learner)).json()["sessionId"]
    seed_exchange(api._test_engine, sid, [("Hi Camden, how are you?", "I get tired fast.")])
    assert api.post(f"/api/sessions/{sid}/complete", headers=bearer(learner)).status_code == 200
    return inst, learner, sid


def _queue_assessment(fake):
    from tests.test_assessment import _combined, _review

    fake.queue_structured(_combined(), _review())


def test_course_interview_reports_minimal_events_once(api):
    inst, learner, sid = _course_interview(api)
    _queue_assessment(api.fake)
    r = api.post(f"/api/sessions/{sid}/assessment", headers=bearer(learner))
    assert r.status_code == 201 and r.json()["status"] == "COMPLETE"
    # Repeating completion and assessment never adds events.
    api.post(f"/api/sessions/{sid}/complete", headers=bearer(learner))
    api.post(f"/api/sessions/{sid}/assessment", headers=bearer(learner))
    events = _outbox(api._test_engine)
    assert [t for t, _ in events] == ["activity.started", "activity.completed", "assessment.completed"]
    with _db(api._test_engine) as db:
        subject = db.execute(select(PlatformIdentity.subject)).scalars().all()
    for _, e in events:
        assert e["offeringId"] == CTX_A and e["subject"] == "platform-sub-1"
        assert e["activityRef"] == "ai-sp/cases/camden" and e["schemaVersion"] == "1.0"
        assert e["subject"] in subject
    assessed = events[2][1]["data"]
    assert assessed["level"] == "Developing" and assessed["attempt"] == 1
    assert assessed["assessmentRef"].startswith("ai-sp/assessments/")
    # Minimum necessary: no transcript, narrative, name, or numeric score.
    blob = json.dumps([e for _, e in events]).lower()
    for banned in ("tired", "camden, how", "summary", "narrative", "score", "synthetic learner"):
        assert banned not in blob
    # The instructor view now shows the qualitative result for this course.
    detail = api.get(f"/api/lti/instructor/sessions/{sid}", headers=bearer(inst)).json()
    assert detail["assessment"]["overallLevel"] == "Developing"


def test_direct_login_sessions_are_never_reported(engine, lti_env):
    with make_client(engine, FakeOpenAIClient(), authenticate=True) as client:
        sid = client.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"}).json()["sessionId"]
        seed_exchange(engine, sid)
        client.post(f"/api/sessions/{sid}/complete")
    assert _outbox(engine) == []


class _FakePlatform:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.batches = []

    def send(self, events):
        self.batches.append(events)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return [{"eventId": e["eventId"], "status": outcome} for e in events]


def test_delivery_retries_then_sends_without_duplicates(api):
    _course_interview(api)
    platform = _FakePlatform([httpx.ConnectError("platform down"), "applied", "duplicate"])
    with _db(api._test_engine) as db:
        assert platform_events.deliver_pending(db, platform) == {"sent": 0, "rejected": 0, "retry": 2}
        rows = db.execute(select(PlatformEventOutbox)).scalars().all()
        assert {(r.status, r.attempts, r.last_error) for r in rows} == {("pending", 1, "ConnectError")}
        # Not due yet: nothing is sent before the backoff expires.
        assert platform_events.deliver_pending(db, platform) == {"sent": 0, "rejected": 0, "retry": 0}
        for r in rows:
            r.next_attempt_at = r.created_at
        db.commit()
        assert platform_events.deliver_pending(db, platform)["sent"] == 2
        assert platform_events.deliver_pending(db, platform)["sent"] == 0  # nothing left
    ids = [e["eventId"] for batch in platform.batches for e in batch]
    assert len(platform.batches) == 2 and ids[:2] == ids[2:]  # same stable ids on retry


def test_rejected_events_are_terminal(api):
    _course_interview(api)
    with _db(api._test_engine) as db:
        assert platform_events.deliver_pending(db, _FakePlatform(["rejected"]))["rejected"] == 2
        assert platform_events.deliver_pending(db, _FakePlatform([]))["sent"] == 0


def test_platform_client_uses_signed_client_assertion(api):
    _course_interview(api)
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path.endswith("/lti/token"):
            form = parse_qs(request.content.decode())
            assertion = form["client_assertion"][0]
            header = jwt.get_unverified_header(assertion)
            claims = jwt.decode(assertion, TOOL_KEY.public_key(), algorithms=["RS256"],
                                audience=f"{ISSUER}/api/v1/lti/token")
            seen["token"] = (header["kid"], claims["iss"], claims["sub"], form["scope"][0])
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 300})
        seen["auth"] = request.headers["authorization"]
        events = json.loads(request.content)["events"]
        return httpx.Response(200, json={"results": [{"eventId": e["eventId"], "status": "applied"} for e in events]})

    client = platform_events.PlatformClient(httpx.Client(transport=httpx.MockTransport(handler)))
    with _db(api._test_engine) as db:
        assert platform_events.deliver_pending(db, client)["sent"] == 2
    assert seen["token"] == ("tool-1", CLIENT_ID, CLIENT_ID, "iexcel.events.write")
    assert seen["auth"] == "Bearer svc-token"


# --------------------------------------------------------- development stand-in


def test_dev_simulated_interview_and_fake_assessment(engine, lti_env, monkeypatch):
    monkeypatch.setenv("AI_SP_DEV_FAKE_AI", "true")
    get_settings.cache_clear()
    with make_client(engine, authenticate=False) as client:  # real get_openai_client
        inst = _launch(client, sub="inst-1", role=INSTRUCTOR)["accessToken"]
        _assign(client, inst, ["jayden"])
        learner = _launch(client)["accessToken"]
        sid = client.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "jayden"},
                          headers=bearer(learner)).json()["sessionId"]
        sim = client.post(f"/api/dev/sessions/{sid}/simulate-interview", headers=bearer(learner))
        assert sim.status_code == 200 and sim.json()["turnsWritten"] == 8
        assert client.post(f"/api/dev/sessions/{sid}/simulate-interview", headers=bearer(learner)).json()["turnsWritten"] == 0
        assert client.post(f"/api/sessions/{sid}/complete", headers=bearer(learner)).status_code == 200
        r = client.post(f"/api/sessions/{sid}/assessment", headers=bearer(learner))
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "COMPLETE" and r.json()["overallLevel"] == "Developing"
    assert [t for t, _ in _outbox(engine)] == ["activity.started", "activity.completed", "assessment.completed"]


def test_dev_simulator_is_unavailable_without_the_switch(api):
    inst = _launch(api, sub="inst-1", role=INSTRUCTOR)["accessToken"]
    _assign(api, inst, ["camden"])
    learner = _launch(api)["accessToken"]
    sid = api.post("/api/sessions", json={"studentName": "x", "studentId": "", "caseId": "camden"},
                   headers=bearer(learner)).json()["sessionId"]
    assert api.post(f"/api/dev/sessions/{sid}/simulate-interview", headers=bearer(learner)).status_code == 404
