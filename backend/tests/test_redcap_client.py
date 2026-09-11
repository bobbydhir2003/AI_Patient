"""REDCap client boundary tests.

Focus: the API token stays server-side and is never logged; timeouts/HTTP
errors are turned into a single generic RedcapError; the request is well-formed.
No real network call is made (httpx.post is monkeypatched).
"""
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.services import redcap_client
from app.services.redcap_client import (
    RedcapError,
    RedcapNotConfiguredError,
    import_record,
    is_configured,
)

SECRET = "SUPER-SECRET-REDCAP-TOKEN"
URL = "https://redcap.example.org/api/"


def _configure(monkeypatch, url=URL, token=SECRET):
    monkeypatch.setattr(
        redcap_client,
        "get_settings",
        lambda: SimpleNamespace(
            redcap_api_url=url, redcap_api_token=token, redcap_timeout_seconds=15.0
        ),
    )


class _FakeResponse:
    def __init__(self, status_code=200, body="1"):
        self.status_code = status_code
        self._body = body

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        return json.loads(self._body)


def test_is_configured_requires_url_and_token(monkeypatch):
    _configure(monkeypatch, token="")
    assert is_configured() is False
    _configure(monkeypatch)
    assert is_configured() is True


def test_import_record_posts_token_in_body_and_returns_none(monkeypatch):
    _configure(monkeypatch)
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse(200, {"count": 1})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = import_record({"record_id": "D1", "pre_conf_begin": 4})
    assert result is None
    assert captured["url"] == URL
    assert captured["timeout"] == 15.0
    # Token travels in the POST body (server-side only), and the data payload is
    # the REDCap record import contract.
    assert captured["data"]["token"] == SECRET
    assert captured["data"]["content"] == "record"
    assert captured["data"]["overwriteBehavior"] == "normal"
    sent = json.loads(captured["data"]["data"])
    assert sent == [{"record_id": "D1", "pre_conf_begin": 4}]


def test_not_configured_raises(monkeypatch):
    _configure(monkeypatch, token="")
    with pytest.raises(RedcapNotConfiguredError):
        import_record({"record_id": "D1"})


def test_http_error_raises_and_never_logs_token(monkeypatch, caplog):
    _configure(monkeypatch)

    def fake_post(url, data=None, timeout=None):
        return _FakeResponse(400, {"error": "bad request"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RedcapError) as exc:
            import_record({"record_id": "D1", "pre_conf_begin": 4})
    assert SECRET not in str(exc.value)
    assert SECRET not in caplog.text


def test_transport_error_raises_and_never_logs_token(monkeypatch, caplog):
    _configure(monkeypatch)

    def fake_post(url, data=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RedcapError) as exc:
            import_record({"record_id": "D1"})
    assert SECRET not in str(exc.value)
    assert SECRET not in caplog.text
