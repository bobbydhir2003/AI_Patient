"""Tests for the P0/P1 OpenAI reliability fix:

P0 - the OpenAI SDK's built-in retries are disabled (max_retries=0) so
     `provider_retry` is the SINGLE retry layer. One logical provider call =
     at most 4 outbound HTTP attempts (1 + provider_max_retries), never 12.
P1 - assessment structured calls use a longer, assessment-specific per-request
     timeout, applied per-call (concurrency-safe) so patient chat keeps its
     default low-latency timeout.

All OpenAI I/O is mocked; no real network requests are made.
"""
import types

import pytest

from app.core.config import get_settings
from app.core.exceptions import PatientEngineError
from app.patient_engine.openai_client import OpenAIPatientClient


class _FakeResponses:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **kwargs):
        self._outer.create_calls.append(kwargs)
        if self._outer.raise_exc is not None:
            raise self._outer.raise_exc()
        return types.SimpleNamespace(
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
            output_text=self._outer.output_text,
        )


class _FakeSDKClient:
    """Stands in for the real `openai.OpenAI` client at the responses boundary."""

    def __init__(self, output_text="{}", raise_exc=None):
        self.create_calls: list[dict] = []
        self.output_text = output_text
        self.raise_exc = raise_exc
        self.responses = _FakeResponses(self)


class _APITimeoutError(Exception):
    """Name matches what provider_retry.classify treats as retryable."""
    __name__ = "APITimeoutError"


# Give the class the exact type name provider_retry looks for.
_APITimeoutError.__name__ = "APITimeoutError"
_APITimeoutError.__qualname__ = "APITimeoutError"


def _rt(timeout=30.0):
    return types.SimpleNamespace(
        api_key="test-key", timeout_seconds=timeout, model="gpt-4o-mini",
        max_output_tokens=400,
    )


def _prime(client: OpenAIPatientClient, fake_sdk, rt):
    """Wire an OpenAIPatientClient to use a fake SDK client + fixed runtime,
    bypassing real OpenAI construction and the mock-AI/db lookup."""
    client._client = fake_sdk
    client._client_fingerprint = (rt.api_key, rt.timeout_seconds)
    client._runtime = lambda *a, **k: rt  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Make provider_retry backoff instant so retry-count tests are fast."""
    s = get_settings()
    monkeypatch.setattr(s, "provider_retry_base_ms", 0)
    monkeypatch.setattr(s, "provider_retry_max_ms", 0)
    # Ensure the mock-AI branch (which would short-circuit the SDK call) is off.
    monkeypatch.setattr("app.patient_engine.openai_client._mock_ai", lambda: False)
    yield


# ----------------------------------------------------------------- P0: retries
def test_sdk_retries_disabled_at_construction(monkeypatch):
    """The real OpenAI() client is built with max_retries=0 (single retry layer)."""
    import openai

    captured: dict = {}

    class _Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _Spy)
    client = OpenAIPatientClient()
    client._get_client(_rt(timeout=30.0))
    assert captured.get("max_retries") == 0, "SDK retries must be disabled"
    assert captured.get("timeout") == 30.0


def test_one_provider_attempt_is_one_sdk_request(monkeypatch):
    """With provider_max_retries=3 and SDK retries off, a persistently timing-out
    call makes exactly 4 outbound SDK requests (1 + 3), NOT 12."""
    s = get_settings()
    monkeypatch.setattr(s, "provider_max_retries", 3)

    fake = _FakeSDKClient(raise_exc=_APITimeoutError)
    client = OpenAIPatientClient()
    _prime(client, fake, _rt())

    with pytest.raises(PatientEngineError):
        client.generate_structured([{"role": "user", "content": "x"}], {}, "combined_assessment")

    assert len(fake.create_calls) == 4  # 1 initial + 3 provider retries; never 12


# ------------------------------------------------------------- P1: timeout sep.
def test_patient_call_uses_default_timeout_no_override(monkeypatch):
    """Patient chat passes NO per-request timeout, so the client-level default
    (openai_timeout_seconds) is used unchanged."""
    fake = _FakeSDKClient(output_text="{}")
    client = OpenAIPatientClient()
    _prime(client, fake, _rt(timeout=30.0))

    client.generate_structured([{"role": "user", "content": "x"}], {}, "patient_reply")

    assert "timeout" not in fake.create_calls[0]


def test_assessment_call_uses_assessment_timeout(monkeypatch):
    """An assessment structured call applies the assessment timeout per-request."""
    fake = _FakeSDKClient(output_text="{}")
    client = OpenAIPatientClient()
    _prime(client, fake, _rt(timeout=30.0))

    client.generate_structured(
        [{"role": "user", "content": "x"}], {}, "combined_assessment", timeout_seconds=90.0
    )

    assert fake.create_calls[0].get("timeout") == 90.0


def test_interleaved_calls_do_not_contaminate_timeouts(monkeypatch):
    """Concurrency safety: the SAME client used for an assessment call (90s) and
    a patient call (default) records each timeout independently - no shared state."""
    fake = _FakeSDKClient(output_text="{}")
    client = OpenAIPatientClient()
    _prime(client, fake, _rt(timeout=30.0))

    client.generate_structured([{"role": "user", "content": "a"}], {}, "combined_assessment", timeout_seconds=90.0)
    client.generate_structured([{"role": "user", "content": "b"}], {}, "patient_reply")
    client.generate_structured([{"role": "user", "content": "c"}], {}, "assessment_review", timeout_seconds=90.0)

    assert fake.create_calls[0].get("timeout") == 90.0
    assert "timeout" not in fake.create_calls[1]
    assert fake.create_calls[2].get("timeout") == 90.0


# --------------------------------------------------- P1: budget wires the value
def test_assessment_call_budget_passes_assessment_timeout(monkeypatch):
    """Every assessment provider call funnels through AssessmentCallBudget, which
    supplies the assessment-specific timeout to the client."""
    import app.assessment.assessment_call_budget as budmod
    from app.assessment.assessment_call_budget import AssessmentCallBudget

    monkeypatch.setattr(budmod.usage_recorder, "record_openai_usage", lambda *a, **k: None)

    captured: dict = {}

    class _RecordingClient:
        def generate_structured(self, *args, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

    budget = AssessmentCallBudget(
        _RecordingClient(), None,
        session_id="s1", student_id=None, case_id="camden", model="gpt-4o-mini",
        max_calls=3,
    )
    budget.generate_structured([{"role": "user", "content": "x"}], {}, "combined_assessment",
                               stage="assessment_generate")

    assert captured.get("timeout_seconds") == get_settings().openai_assessment_timeout_seconds
    assert get_settings().openai_assessment_timeout_seconds == 90.0
