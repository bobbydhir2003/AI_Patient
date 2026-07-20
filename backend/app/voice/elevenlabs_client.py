"""Thin wrapper around the ElevenLabs text-to-speech streaming API.

Security rules enforced here:
- The API key comes only from backend settings and goes only into the
  `xi-api-key` request header. It is never logged and never included in any
  error message or response.
- Errors surface to the frontend only as the generic VoiceSynthesisError; the
  failure *category* (timeout / auth / rate_limit / api) is logged server-side.

Latency: a single module-level httpx.Client is shared across requests so TCP
and TLS connections to api.elevenlabs.io are kept alive and reused between
patient turns (a fresh handshake per turn previously cost ~100-300 ms). The
client is thread-safe and is closed by the FastAPI lifespan on shutdown.
"""
import threading
import time
from collections.abc import Iterator

import httpx

from app.core.config import get_settings
from app.core.exceptions import VoiceSynthesisError
from app.core.logging import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.elevenlabs.io/v1"
# MP3 output formats map to audio/mpeg; extend here if PCM formats are ever used.
MEDIA_TYPES = {"mp3": "audio/mpeg"}

# ---- Shared HTTP client (keep-alive across patient turns) ----
_http_client: httpx.Client | None = None
_http_client_lock = threading.Lock()


def get_http_client() -> httpx.Client:
    """Return the shared, thread-safe httpx client (created lazily).

    Connection reuse: keep-alive connections persist between turns, so only
    the first request of a session pays the TCP+TLS handshake.
    """
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            settings = get_settings()
            _http_client = httpx.Client(
                timeout=httpx.Timeout(settings.elevenlabs_timeout_seconds, connect=10.0),
                limits=httpx.Limits(
                    max_keepalive_connections=4,
                    max_connections=8,
                    keepalive_expiry=120.0,
                ),
            )
        return _http_client


def close_http_client() -> None:
    """Close the shared client (called from the FastAPI shutdown lifespan)."""
    global _http_client
    with _http_client_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None


def media_type_for(output_format: str) -> str:
    prefix = output_format.split("_", 1)[0]
    return MEDIA_TYPES.get(prefix, "audio/mpeg")


def _failure_category(exc: Exception, status_code: int | None = None) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if isinstance(exc, httpx.TransportError):
        return "connection"
    return "api"


class ElevenLabsClient:
    """Streams synthesized speech for already-approved patient text."""

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self._settings.elevenlabs_api_key) and self._settings.elevenlabs_enabled

    def stream_speech(
        self,
        *,
        text: str,
        voice_id: str,
        model_id: str,
        voice_settings: dict,
        output_format: str | None = None,
    ) -> Iterator[bytes]:
        """Yield audio chunks from the ElevenLabs streaming endpoint.

        Chunks are yielded as they arrive from upstream (no full-response
        buffering). Raises VoiceSynthesisError (safe, generic message) on any
        failure.
        """
        fmt = output_format or self._settings.elevenlabs_output_format
        url = f"{_BASE_URL}/text-to-speech/{voice_id}/stream"
        headers = {"xi-api-key": self._settings.elevenlabs_api_key}  # never logged
        body = {
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        debug = self._settings.debug
        t_start = time.monotonic()

        try:
            client = get_http_client()  # shared keep-alive client; NOT closed here
            with client.stream(
                "POST", url, headers=headers, json=body, params={"output_format": fmt}
            ) as response:
                if debug:
                    logger.info(
                        "tts_timing upstream_headers_ms=%.0f voice_id=%s",
                        (time.monotonic() - t_start) * 1000, voice_id,
                    )
                if response.status_code >= 400:
                    # Read (bounded) body for the server log only.
                    detail = response.read()[:300]
                    category = _failure_category(Exception(), response.status_code)
                    logger.warning(
                        "elevenlabs_error category=%s status=%d voice_id=%s detail=%r",
                        category, response.status_code, voice_id, detail,
                    )
                    raise VoiceSynthesisError()
                yielded = False
                for chunk in response.iter_bytes():
                    if chunk:
                        if not yielded and debug:
                            logger.info(
                                "tts_timing upstream_first_chunk_ms=%.0f voice_id=%s",
                                (time.monotonic() - t_start) * 1000, voice_id,
                            )
                        yielded = True
                        yield chunk
                if not yielded:
                    logger.warning("elevenlabs_error category=empty_response voice_id=%s", voice_id)
                    raise VoiceSynthesisError()
                if debug:
                    logger.info(
                        "tts_timing upstream_complete_ms=%.0f voice_id=%s",
                        (time.monotonic() - t_start) * 1000, voice_id,
                    )
        except VoiceSynthesisError:
            raise
        except httpx.HTTPError as exc:
            category = _failure_category(exc)
            # str(exc) may contain the URL but never the key (header-based auth).
            logger.warning("elevenlabs_error category=%s voice_id=%s error=%s", category, voice_id, exc)
            raise VoiceSynthesisError() from exc


_default_client: ElevenLabsClient | None = None


def get_elevenlabs_client() -> ElevenLabsClient:
    global _default_client
    if _default_client is None:
        _default_client = ElevenLabsClient()
    return _default_client
