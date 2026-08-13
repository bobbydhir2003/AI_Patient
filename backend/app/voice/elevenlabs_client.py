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


def _mock_ai() -> bool:
    """Effective simulated-provider flag (startup env OR load-test runtime override)."""
    try:
        from app.services import runtime_config_service
        return runtime_config_service.mock_ai_enabled()
    except Exception:
        return bool(get_settings().mock_ai)


class _TransientTTSError(Exception):
    """Internal marker for a retryable TTS failure (429/5xx/timeout/empty)."""

    def __init__(self, rate_limited: bool = False):
        super().__init__("transient tts failure")
        self.rate_limited = rate_limited

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

    Pool sizing: max_connections is derived from the configured TTS
    concurrency (never hard-coded smaller than the semaphore that gates
    entry) so a request that already won a TTS slot can never then queue
    again inside httpx for a free connection. keepalive_connections is a
    bounded portion of that so idle connections don't pin an unreasonable
    number of sockets open.
    """
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            settings = get_settings()
            max_connections = max(
                settings.max_concurrent_tts_requests, settings.elevenlabs_pool_min_connections
            )
            max_keepalive = max(4, max_connections // 2)
            _http_client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=settings.elevenlabs_timeout_seconds,
                    write=10.0,
                    # A request that passed the TTS semaphore must fail fast,
                    # not hang, if the pool is somehow still exhausted.
                    pool=settings.elevenlabs_pool_timeout_seconds,
                ),
                limits=httpx.Limits(
                    max_keepalive_connections=max_keepalive,
                    max_connections=max_connections,
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
    """Streams synthesized speech for already-approved patient text.

    The API key + output format are read from the RuntimeConfigurationService on
    each request, so replacing the key or changing the format via the dashboard
    applies immediately to the next TTS request. The key stays server-side (sent
    only as the xi-api-key header, never logged)."""

    def __init__(self) -> None:
        self._settings = get_settings()

    @staticmethod
    def _runtime():
        from app.services import runtime_config_service
        return runtime_config_service.elevenlabs_runtime()

    @property
    def configured(self) -> bool:
        rt = self._runtime()
        return bool(rt.api_key) and rt.enabled

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
        settings = get_settings()
        if _mock_ai():
            yield from self._mock_stream()
            return

        rt = self._runtime()
        fmt = output_format or rt.output_format
        url = f"{_BASE_URL}/text-to-speech/{voice_id}/stream"
        headers = {"xi-api-key": rt.api_key}  # never logged
        body = {
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        debug = self._settings.debug
        t_start = time.monotonic()

        from app.core.telemetry import get_telemetry

        tele = get_telemetry()
        tele.elevenlabs.active.inc()
        # Retry only the connection + first chunk (transient network/429/5xx);
        # a mid-stream failure is not retried. The endpoint degrades to text-only
        # if this ultimately fails, so the interview turn is never lost.
        attempts = settings.provider_max_retries
        try:
            last_exc: Exception | None = None
            for attempt in range(attempts + 1):
                try:
                    yield from self._stream_once(url, headers, body, fmt, voice_id, debug, t_start, tele)
                    tele.elevenlabs.record(latency_ms=(time.monotonic() - t_start) * 1000, ok=True)
                    return
                except _TransientTTSError as exc:
                    last_exc = exc
                    if exc.rate_limited:
                        tele.elevenlabs.window.incr("rate_limited")
                    if attempt < attempts:
                        import random
                        delay = min(
                            settings.provider_retry_base_ms * (2 ** attempt) + random.uniform(0, settings.provider_retry_base_ms),
                            settings.provider_retry_max_ms,
                        ) / 1000.0
                        tele.elevenlabs.window.incr("retries")
                        time.sleep(delay)
                        continue
                    break
            tele.elevenlabs.record(latency_ms=(time.monotonic() - t_start) * 1000, ok=False)
            logger.warning(
                "tts_provider_failure voice_id=%s attempts=%d category=%s",
                voice_id, attempts + 1,
                "rate_limit" if isinstance(last_exc, _TransientTTSError) and last_exc.rate_limited else "provider",
            )
            raise VoiceSynthesisError() from last_exc
        finally:
            tele.elevenlabs.active.dec()

    def _stream_once(self, url, headers, body, fmt, voice_id, debug, t_start, tele):
        """One connection attempt. Raises _TransientTTSError for retryable
        failures BEFORE any chunk is yielded; once bytes flow, a failure ends the
        clip (no retry)."""
        client = get_http_client()  # shared keep-alive client; NOT closed here
        try:
            with client.stream(
                "POST", url, headers=headers, json=body, params={"output_format": fmt}
            ) as response:
                if response.status_code >= 400:
                    detail = response.read()[:300]
                    category = _failure_category(Exception(), response.status_code)
                    logger.warning(
                        "elevenlabs_error category=%s status=%d voice_id=%s detail=%r",
                        category, response.status_code, voice_id, detail,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _TransientTTSError(rate_limited=response.status_code == 429)
                    raise VoiceSynthesisError()
                yielded = False
                for chunk in response.iter_bytes():
                    if chunk:
                        yielded = True
                        yield chunk
                if not yielded:
                    logger.warning("elevenlabs_error category=empty_response voice_id=%s", voice_id)
                    raise _TransientTTSError(rate_limited=False)
        except VoiceSynthesisError:
            raise
        except _TransientTTSError:
            raise
        except httpx.HTTPError as exc:
            category = _failure_category(exc)
            logger.warning("elevenlabs_error category=%s voice_id=%s error=%s", category, voice_id, exc)
            # Timeouts / transport errors before completion are transient.
            raise _TransientTTSError(rate_limited=False) from exc

    def _mock_stream(self):
        """Canned audio for MOCK_AI load-test mode (configurable latency)."""
        from app.core.telemetry import get_telemetry

        tele = get_telemetry()
        tele.elevenlabs.active.inc()
        t0 = time.monotonic()
        try:
            time.sleep(get_settings().mock_tts_latency_ms / 1000.0)
            yield b"ID3mockmp3"
            yield b"audio-bytes"
            tele.elevenlabs.record(latency_ms=(time.monotonic() - t0) * 1000, ok=True)
        finally:
            tele.elevenlabs.active.dec()


_default_client: ElevenLabsClient | None = None


def get_elevenlabs_client() -> ElevenLabsClient:
    global _default_client
    if _default_client is None:
        _default_client = ElevenLabsClient()
    return _default_client
