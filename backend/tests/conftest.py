import os

os.environ.setdefault("ENVIRONMENT", "development")  # tests run in dev (non-strict) mode
os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory; set before app imports
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("AUTO_CREATE_TABLES", "false")
os.environ.setdefault("OPENAI_MAX_RETRIES", "0")
# Abuse guards are exercised by dedicated tests that enable them explicitly;
# keep them off by default so the broad suite is not throttled.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LOGIN_THROTTLE_ENABLED", "false")
# Tests must not inherit the developer .env streaming flag; the streaming suite
# enables it explicitly via the `streaming_enabled` fixture. (env overrides .env)
os.environ.setdefault("OPENAI_PATIENT_STREAMING_ENABLED", "false")
# Background threads (telemetry sampler + assessment worker) are driven
# deterministically in tests; don't auto-start them from the app lifespan.
os.environ.setdefault("BACKGROUND_WORKERS_ENABLED", "false")
# The broad suite exercises assessment SYNCHRONOUSLY; the dedicated queue tests
# opt into the background queue explicitly.
os.environ.setdefault("ASSESSMENT_QUEUE_ENABLED", "false")

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.exceptions import PatientEngineError
from app.database.base import Base
from app.database.connection import get_db
from app.main import create_app
from app.patient_engine.openai_client import get_openai_client
from app.schemas.interview_schema import PatientReply


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


class FakeOpenAIClient:
    """Test double at the OpenAI boundary. Returns structured PatientReply objects."""

    def __init__(
        self,
        text: str = "Okay.",
        response_type: str = "clinical_answer",
        used_fact_ids: list[str] | None = None,
        fail: bool = False,
        configured: bool = True,
    ):
        self.text = text
        self.response_type = response_type
        self.used_fact_ids = used_fact_ids or []
        self.fail = fail
        self.configured = configured
        self.calls: list[list[dict]] = []

    def generate(self, messages: list[dict]) -> PatientReply:
        self.calls.append(messages)
        if self.fail or not self.configured:
            raise PatientEngineError("simulated OpenAI failure")
        return PatientReply(
            patient_text=self.text,
            used_fact_ids=list(self.used_fact_ids),
            response_type=self.response_type,
            supported=True,
        )

    # --- structured-output test double (assessment pipeline) ---
    structured_queue: list | None = None

    def queue_structured(self, *payloads: dict) -> None:
        if self.structured_queue is None:
            self.structured_queue = []
        self.structured_queue.extend(payloads)

    def generate_structured(self, messages, schema, schema_name, max_output_tokens=None, **kwargs) -> dict:
        self.calls.append(messages)
        if self.fail or not self.configured:
            raise PatientEngineError("simulated OpenAI failure")
        if not self.structured_queue:
            raise PatientEngineError("no queued structured response for " + schema_name)
        return self.structured_queue.pop(0)


@pytest.fixture()
def fake_client():
    return FakeOpenAIClient(text="I get tired a lot faster than I used to.")


def seed_default_student(engine, *, name="Default Student", number="D1", email="default@school.edu"):
    """Create a Student + linked student User directly in the DB and return the
    user id. Used to auto-authenticate happy-path clients."""
    from app.core.security import hash_password
    from app.models import Student, User

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        student = Student(name=name, student_number=number, email=email)
        db.add(student)
        db.flush()
        user = User(
            email=email,
            password_hash=hash_password("defaultpass1"),
            full_name=name,
            student_number=number,
            role="student",
            student_id=student.id,
            is_active=True,
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def make_client(engine, openai_client=None, *, authenticate=True):
    """Build a TestClient.

    authenticate=True (default): a default student account is seeded and requests
    WITHOUT an Authorization header are treated as that student, so the many
    happy-path tests keep working now that endpoints require auth. Requests that
    DO carry a bearer token are resolved for real (so ownership / 401 / negative
    tests still exercise the true auth path). Pass authenticate=False for a fully
    unauthenticated client (used by auth/security tests).
    """
    from app.dependencies.auth import get_current_user as real_get_current_user

    app = create_app()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    if openai_client is not None:
        app.dependency_overrides[get_openai_client] = lambda: openai_client

    if authenticate:
        from fastapi import Request
        from sqlalchemy.orm import Session as _Session

        from app.models import User

        default_uid = seed_default_student(engine)

        def override_current_user(request: Request, db: _Session = Depends(get_db)) -> User:
            # Honor a real bearer token when present (ownership/negative tests);
            # otherwise act as the seeded default student (happy-path tests).
            if request.headers.get("Authorization") or request.headers.get("authorization"):
                return real_get_current_user(request, db)
            return db.get(User, default_uid)

        app.dependency_overrides[real_get_current_user] = override_current_user

    tc = TestClient(app)
    tc._test_engine = engine  # lets helpers flip account_status directly
    return tc


@pytest.fixture()
def client(engine, fake_client):
    """Unauthenticated API client with a WORKING fake OpenAI boundary. Used by
    auth tests that assert 401s; other tests use authenticated clients."""
    with make_client(engine, fake_client, authenticate=False) as test_client:
        yield test_client


@pytest.fixture()
def failing_client(engine):
    """Unauthenticated API client whose OpenAI boundary always fails."""
    with make_client(engine, FakeOpenAIClient(fail=True), authenticate=False) as test_client:
        yield test_client


@pytest.fixture()
def student_client(engine, fake_client):
    """Authenticated (default-student) client with a working fake OpenAI."""
    with make_client(engine, fake_client, authenticate=True) as test_client:
        yield test_client


@pytest.fixture()
def failing_student_client(engine):
    """Authenticated (default-student) client whose OpenAI boundary fails."""
    with make_client(engine, FakeOpenAIClient(fail=True), authenticate=True) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_abuse_guards():
    """Keep the process-local rate limiter / login throttle from leaking counts
    across tests."""
    from app.core.rate_limit import reset_login_throttle, reset_rate_limits
    from app.core.telemetry import reset_telemetry

    reset_rate_limits()
    reset_login_throttle()
    reset_telemetry()
    yield
    reset_rate_limits()
    reset_login_throttle()
    reset_telemetry()


# --------------------------------------------------------------------------
#  Shared authentication helpers (endpoints now require a bearer token).
# --------------------------------------------------------------------------
_STUDENT_SEQ = {"n": 0}


def register_student(
    client, *, full_name="Test Student", email=None, password="studpass1", number="S1"
):
    """Register a student, APPROVE it (registration now creates PENDING accounts),
    log in, and return the TokenResponse JSON. This keeps the many happy-path
    tests working; the D-specific tests exercise the real pending/approval flow
    directly against the endpoints."""
    if email is None:
        _STUDENT_SEQ["n"] += 1
        email = f"student{_STUDENT_SEQ['n']}@school.edu"
    resp = client.post(
        "/api/auth/register",
        json={
            "fullName": full_name,
            "email": email,
            "password": password,
            "studentNumber": number,
        },
    )
    assert resp.status_code == 201, resp.text
    # Simulate admin approval so the helper yields a usable, signed-in student.
    engine = getattr(client, "_test_engine", None)
    if engine is not None:
        from sqlalchemy.orm import sessionmaker
        from app.models import User

        db = sessionmaker(bind=engine)()
        try:
            u = db.query(User).filter(User.email == email.strip().lower()).first()
            if u is not None:
                u.account_status = "ACTIVE"
                u.is_active = True
                db.commit()
        finally:
            db.close()
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()


def auth_headers(client, **kwargs) -> dict:
    """Register a student and return an Authorization header dict for them."""
    body = register_student(client, **kwargs)
    return {"Authorization": f"Bearer {body['accessToken']}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
