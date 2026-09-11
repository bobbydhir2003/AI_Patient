"""Thin, isolated REDCap API client.

Design goals (see the surveys feature spec):
- The REDCap API token lives ONLY server-side and NEVER leaves this module: it
  is read from settings, sent in the POST body to REDCap, and is never logged,
  returned, or included in any exception message.
- All REDCap access goes through this one client instead of being scattered
  across controllers/services.
- Bounded timeout + safe error handling: transient REDCap problems raise a
  single, generic ``RedcapError`` that the caller turns into a clean student
  message. We never leak provider internals.

Only the operations this feature needs are implemented: importing (upserting) a
single record's fields for one instrument.
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# REDCap [instrument]_complete status codes.
REDCAP_COMPLETE = 2  # 0 = Incomplete, 1 = Unverified, 2 = Complete


class RedcapError(Exception):
    """A REDCap import could not be completed (network, timeout, HTTP error, or
    REDCap-reported error). Deliberately generic - never carries the token or
    raw provider payloads beyond a short, sanitized detail for server logs."""


class RedcapNotConfiguredError(RedcapError):
    """REDCap URL/token are not configured. Lets callers decide whether to
    treat sync as skipped rather than a hard failure."""


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.redcap_api_url and settings.redcap_api_token)


def import_record(fields: dict[str, object]) -> None:
    """Upsert a single REDCap record.

    ``fields`` must already contain ``record_id`` and use exact REDCap variable
    names (the caller is responsible for validation/mapping). Uses
    ``overwriteBehavior=normal`` so only the provided fields are written; other
    fields on the record are left untouched (this is what lets Pre and Post write
    to the same record without clobbering each other).

    Raises RedcapNotConfiguredError if url/token are unset, or RedcapError on any
    transport/HTTP/REDCap error. Never raises with the token in the message.
    """
    settings = get_settings()
    if not is_configured():
        raise RedcapNotConfiguredError("REDCap API is not configured.")

    import json as _json

    payload = {
        "token": settings.redcap_api_token,
        "content": "record",
        "format": "json",
        "type": "flat",
        "overwriteBehavior": "normal",
        "forceAutoNumber": "false",
        "returnContent": "count",
        "returnFormat": "json",
        "data": _json.dumps([fields]),
    }

    record_id = fields.get("record_id")
    try:
        response = httpx.post(
            settings.redcap_api_url,
            data=payload,
            timeout=settings.redcap_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        # Log the exception TYPE only - never the payload (it contains the token).
        logger.warning(
            "redcap_import_transport_error record_id=%s error=%s",
            record_id, type(exc).__name__,
        )
        raise RedcapError("REDCap request failed.") from exc

    if response.status_code != 200:
        # REDCap returns 400/403 with a JSON {"error": "..."} body. Log the
        # status + short error text (no token is echoed back by REDCap).
        detail = _safe_error_detail(response)
        logger.warning(
            "redcap_import_http_error record_id=%s status=%s detail=%s",
            record_id, response.status_code, detail,
        )
        raise RedcapError("REDCap rejected the submission.")

    logger.info("redcap_import_ok record_id=%s", record_id)


def _safe_error_detail(response: httpx.Response) -> str:
    """Best-effort short error string from a REDCap error response. Truncated so
    a large HTML error page can't flood logs. Never contains the request token
    (that is only ever in the request body, never echoed in REDCap responses)."""
    try:
        body = response.json()
        if isinstance(body, dict) and "error" in body:
            return str(body["error"])[:200]
    except Exception:
        pass
    return (response.text or "")[:200]
