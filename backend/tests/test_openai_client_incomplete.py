"""OpenAIPatientClient: incomplete/truncated Responses API handling.

The assessment pipeline hands the model a large four-domain job. When the model
hits the output-token ceiling the Responses API returns status="incomplete"
with a cut-off body. These tests prove the client:

  * classifies an incomplete response as truncation (never passes a half-formed
    payload on to schema/rubric validation), and
  * performs exactly ONE bounded truncation retry (with more tokens) - a second
    incomplete result fails cleanly rather than looping.

No network: the OpenAI SDK client and runtime are faked.
"""
import types

import pytest

import app.patient_engine.openai_client as oc
from app.core.exceptions import StructuredOutputTruncatedError


def _resp(*, status, output_text="", reason=None):
    return types.SimpleNamespace(
        status=status,
        incomplete_details=types.SimpleNamespace(reason=reason) if reason else None,
        output_text=output_text,
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class _FakeSDK:
    """Stands in for the OpenAI SDK client; scripts responses.create outputs and
    records the max_output_tokens each call requested."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.token_requests: list[int] = []
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.token_requests.append(kwargs.get("max_output_tokens"))
        if not self._scripted:
            raise AssertionError("responses.create called more times than scripted")
        return self._scripted.pop(0)


@pytest.fixture()
def patched_client(monkeypatch):
    monkeypatch.setattr(oc, "_mock_ai", lambda: False)
    rt = types.SimpleNamespace(
        api_key="sk-test", model="gpt-x", timeout_seconds=30.0, max_output_tokens=1000,
    )
    monkeypatch.setattr(oc.OpenAIPatientClient, "_runtime", staticmethod(lambda: rt))
    client = oc.OpenAIPatientClient()
    return client, monkeypatch


def test_incomplete_response_triggers_one_truncation_retry(patched_client):
    client, monkeypatch = patched_client
    sdk = _FakeSDK([
        _resp(status="incomplete", reason="max_output_tokens", output_text=""),
        _resp(status="completed", output_text='{"ok": true}'),
    ])
    monkeypatch.setattr(client, "_get_client", lambda rt=None: sdk)

    out = client.generate_structured(
        [{"role": "user", "content": "x"}], {"type": "object"}, "combined_assessment",
        max_output_tokens=1000, allow_truncation_retry=True,
    )
    assert out == {"ok": True}
    # Exactly two attempts, the second asking for MORE tokens (1000 -> 2000).
    assert sdk.token_requests == [1000, 2000]


def test_two_incomplete_responses_fail_cleanly_without_looping(patched_client):
    client, monkeypatch = patched_client
    sdk = _FakeSDK([
        _resp(status="incomplete", reason="max_output_tokens"),
        _resp(status="incomplete", reason="max_output_tokens"),
    ])
    monkeypatch.setattr(client, "_get_client", lambda rt=None: sdk)

    with pytest.raises(StructuredOutputTruncatedError):
        client.generate_structured(
            [{"role": "user", "content": "x"}], {"type": "object"}, "combined_assessment",
            max_output_tokens=1000, allow_truncation_retry=True,
        )
    # Bounded: the retry happens at most once, so create is called exactly twice.
    assert sdk.token_requests == [1000, 2000]


def test_completed_response_is_not_treated_as_truncated(patched_client):
    client, monkeypatch = patched_client
    sdk = _FakeSDK([_resp(status="completed", output_text='{"ok": true}')])
    monkeypatch.setattr(client, "_get_client", lambda rt=None: sdk)

    out = client.generate_structured(
        [{"role": "user", "content": "x"}], {"type": "object"}, "combined_assessment",
        max_output_tokens=1000, allow_truncation_retry=True,
    )
    assert out == {"ok": True}
    assert sdk.token_requests == [1000]  # no retry
