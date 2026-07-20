import os

os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory; set before app imports
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("AUTO_CREATE_TABLES", "false")
os.environ.setdefault("OPENAI_MAX_RETRIES", "0")

import pytest
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


def make_client(engine, openai_client=None):
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
    return TestClient(app)


@pytest.fixture()
def client(engine, fake_client):
    """API client with a WORKING fake OpenAI boundary."""
    with make_client(engine, fake_client) as test_client:
        yield test_client


@pytest.fixture()
def failing_client(engine):
    """API client whose OpenAI boundary always fails."""
    with make_client(engine, FakeOpenAIClient(fail=True)) as test_client:
        yield test_client
