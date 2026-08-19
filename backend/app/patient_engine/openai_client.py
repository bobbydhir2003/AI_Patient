"""Thin wrapper around the OpenAI Python SDK (Responses API, structured output).

Instrumented for Priority B: every real network call goes through
provider_retry.call_with_retry, which retries transient failures (429/5xx/
timeouts) with backoff+jitter and records telemetry (active, requests, 429s,
retries, latency). A MOCK_AI mode returns canned output with configurable
latency so the traffic architecture can be load-tested without spending.
"""
import json
import time

from app.core.config import get_settings
from app.core.exceptions import PatientEngineError, StructuredOutputTruncatedError
from app.core.logging import get_logger
from app.schemas.interview_schema import PATIENT_REPLY_JSON_SCHEMA, PatientReply

logger = get_logger(__name__)

# Canned patient reply used only in MOCK_AI load-test mode.
_MOCK_PATIENT_REPLY = {
    "patient_text": "I've been getting more tired than usual, and it's been on my mind.",
    "used_fact_ids": [],
    "response_type": "clinical_answer",
    "supported": True,
    "speech": None,
}
_MOCK_STREAM_TEXT = (
    "I've been getting more tired than usual lately. "
    "It started a few weeks ago and it's been on my mind."
)


def _mock_ai() -> bool:
    """Effective simulated-provider flag (startup env OR load-test runtime override)."""
    try:
        from app.services import runtime_config_service
        return runtime_config_service.mock_ai_enabled()
    except Exception:
        return bool(get_settings().mock_ai)


class OpenAIPatientClient:
    """Generates structured simulated-patient responses via the OpenAI platform.

    Reads its active model / timeout / key from the RuntimeConfigurationService
    on each request, so an admin changing the model or replacing the key through
    the dashboard takes effect on the NEXT request without a restart. The
    underlying OpenAI SDK client is cached and rebuilt only when the key or
    timeout actually change."""

    def __init__(self) -> None:
        self._client = None
        self._client_fingerprint: tuple | None = None

    @staticmethod
    def _runtime():
        from app.services import runtime_config_service
        return runtime_config_service.openai_runtime()

    @property
    def configured(self) -> bool:
        return bool(self._runtime().api_key)

    def _get_client(self, rt=None):
        rt = rt or self._runtime()
        fingerprint = (rt.api_key, rt.timeout_seconds)
        if self._client is None or self._client_fingerprint != fingerprint:
            from openai import OpenAI  # imported lazily so tests never need a key

            # max_retries=0 (P0 fix): the OpenAI SDK's built-in retries (default 2)
            # are DISABLED so provider_retry is the SINGLE retry layer. Without
            # this, one logical call fanned out to provider_retry(4) x SDK(3) = 12
            # outbound attempts; now it is exactly one HTTP attempt per provider
            # attempt. `timeout` here is the client-level default (patient chat);
            # assessment calls override it per-request via `timeout_seconds` below.
            self._client = OpenAI(
                api_key=rt.api_key, timeout=rt.timeout_seconds, max_retries=0
            )
            self._client_fingerprint = fingerprint
        return self._client

    def _do_generate(self, messages: list[dict], schema: dict, schema_name: str, resolved_tokens: int, usage_out: dict | None = None, timeout_seconds: float | None = None) -> dict:
        settings = get_settings()
        if _mock_ai():
            if usage_out is not None:
                usage_out["input_tokens"] = 150
                usage_out["output_tokens"] = 60
                usage_out["model"] = "mock"
            return self._mock_structured(schema_name)
        rt = self._runtime()
        client = self._get_client(rt)

        from app.core import provider_retry
        from app.core.telemetry import get_telemetry

        # Per-request timeout override (P1): when a caller (assessment path) passes
        # timeout_seconds, it applies to THIS request only via the SDK's per-call
        # `timeout` kwarg - no shared mutable client state, so concurrent patient
        # and assessment requests never contaminate each other's timeout. When
        # None (patient chat), the client-level default timeout is used unchanged.
        request_opts = {"timeout": timeout_seconds} if timeout_seconds is not None else {}

        def _call():
            return client.responses.create(
                model=rt.model,
                input=messages,
                max_output_tokens=resolved_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
                **request_opts,
            )

        response = provider_retry.call_with_retry(
            _call, provider=get_telemetry().openai,
            max_retries=settings.provider_max_retries,
            base_ms=settings.provider_retry_base_ms, max_ms=settings.provider_retry_max_ms,
        )
        # SINGLE token-recording path: use the provider-reported usage from the
        # completed (non-streaming) response. Never estimated.
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            get_telemetry().openai.record_tokens(in_tok, out_tok)
            # Surface provider-reported usage to the caller for per-session
            # cost/usage recording (never estimated from string length).
            if usage_out is not None:
                usage_out["input_tokens"] = in_tok
                usage_out["output_tokens"] = out_tok
                usage_out["model"] = rt.model
        raw = (response.output_text or "").strip()
        if not raw:
            raise PatientEngineError("OpenAI returned an empty response.")
        
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if "Unterminated string" in str(exc) or "Expecting value" in str(exc):
                logger.warning("structured_output_truncated task=%s tokens=%s", schema_name, resolved_tokens)
                raise StructuredOutputTruncatedError("The AI response was truncated before completion.") from exc
            logger.warning("OpenAI request failed: %s", exc)
            raise PatientEngineError(str(exc)) from exc
        except Exception as exc:
            logger.warning("OpenAI request failed: %s", exc)
            raise PatientEngineError(str(exc)) from exc

    @staticmethod
    def _mock_structured(schema_name: str) -> dict:
        """Canned structured output for MOCK_AI load-test mode (no network)."""
        time.sleep(get_settings().mock_model_latency_ms / 1000.0)
        # Synthetic-but-labelled token usage so capacity tracking is exercisable
        # in mock mode (clearly not real provider usage).
        from app.core.telemetry import get_telemetry as _gt
        _gt().openai.record_tokens(150, 60)
        if schema_name == "patient_reply":
            return dict(_MOCK_PATIENT_REPLY)
        return {}

    def generate_structured(
        self,
        messages: list[dict],
        schema: dict,
        schema_name: str,
        max_output_tokens: int | None = None,
        allow_truncation_retry: bool = False,
        usage_out: dict | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        """Generic structured-output call (Responses API, strict JSON schema).

        `usage_out` (optional) is filled with provider-reported input/output
        tokens + model on success, for per-session usage/cost recording.
        `timeout_seconds` (optional) overrides the per-request OpenAI timeout for
        THIS call only - the assessment path passes its own longer timeout while
        patient chat leaves it None and keeps the client-level default."""
        rt = self._runtime()
        if not rt.api_key and not _mock_ai():
            raise PatientEngineError("OPENAI_API_KEY is not configured.")

        resolved_tokens = max_output_tokens or rt.max_output_tokens
        try:
            return self._do_generate(messages, schema, schema_name, resolved_tokens, usage_out=usage_out, timeout_seconds=timeout_seconds)
        except StructuredOutputTruncatedError:
            if allow_truncation_retry:
                retry_tokens = resolved_tokens + 1000
                logger.warning(
                    "truncation_retry_triggered task=%s initial_tokens=%s new_tokens=%s",
                    schema_name, resolved_tokens, retry_tokens
                )
                return self._do_generate(messages, schema, schema_name, retry_tokens, usage_out=usage_out, timeout_seconds=timeout_seconds)
            raise
        except PatientEngineError:
            raise
        except Exception as exc:  # network, auth, rate limits...
            logger.warning("OpenAI request failed: %s", exc)
            raise PatientEngineError(str(exc)) from exc

    def stream_text(
        self,
        messages: list[dict],
        max_output_tokens: int | None = None,
        usage_out: dict | None = None,
    ):
        """Yield plain-text deltas from ONE streaming Responses API request.

        Used by the low-latency patient pipeline (no strict JSON schema - the
        streaming prompt frames the output as text + a metadata tail). Closing
        the returned generator (cancellation/interruption) closes the
        underlying HTTP stream. On completion, `usage_out` (if provided) is
        filled with input/output token counts for dev cost tracking.
        """
        settings = get_settings()
        if _mock_ai():
            return self._mock_stream(usage_out)
        rt = self._runtime()
        if not rt.api_key:
            raise PatientEngineError("OPENAI_API_KEY is not configured.")
        resolved_tokens = (
            max_output_tokens
            or rt.patient_max_output_tokens
            or rt.max_output_tokens
        )
        client = self._get_client(rt)

        from app.core import provider_retry
        from app.core.telemetry import get_telemetry

        tele = get_telemetry()

        # Retry only the STREAM START (before any delta) - safe to replay; a
        # mid-stream failure is never retried (would duplicate output).
        def _start():
            return client.responses.create(
                model=rt.model, input=messages, max_output_tokens=resolved_tokens, stream=True,
            )

        try:
            stream = provider_retry.call_with_retry(
                _start, provider=tele.openai, max_retries=settings.provider_max_retries,
                base_ms=settings.provider_retry_base_ms, max_ms=settings.provider_retry_max_ms,
            )
        except Exception as exc:  # connection/auth/rate-limit before streaming
            logger.warning("OpenAI streaming request failed to start: %s", exc)
            raise PatientEngineError(str(exc)) from exc

        def _deltas():
            try:
                for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            yield delta
                    elif event_type == "response.completed":
                        response = getattr(event, "response", None)
                        usage = getattr(response, "usage", None)
                        if usage is not None:
                            in_tok = getattr(usage, "input_tokens", None)
                            out_tok = getattr(usage, "output_tokens", None)
                            if usage_out is not None:
                                usage_out["input_tokens"] = in_tok
                                usage_out["output_tokens"] = out_tok
                            # SINGLE token-recording path for streaming: real
                            # provider usage from the completed event.
                            from app.core.telemetry import get_telemetry as _gt
                            _gt().openai.record_tokens(in_tok or 0, out_tok or 0)
                    elif event_type in ("response.failed", "error"):
                        raise PatientEngineError("OpenAI streaming response failed.")
            except PatientEngineError:
                raise
            except GeneratorExit:
                raise  # cancelled by the consumer; cleaned up in finally
            except Exception as exc:
                logger.warning("OpenAI streaming request failed: %s", exc)
                raise PatientEngineError(str(exc)) from exc
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # already closed / transport gone
                        pass

        return _deltas()

    def _mock_stream(self, usage_out: dict | None):
        """Canned streamed deltas for MOCK_AI mode (text + metadata tail)."""
        from app.patient_engine.prompt_builder import STREAM_METADATA_DELIMITER

        settings = get_settings()
        meta = {
            "used_fact_ids": [], "response_type": "clinical_answer", "supported": True,
            "speech": {"emotion": "neutral", "pace": "normal", "energy": "normal",
                       "hesitation": "none", "pause_before_ms": 150},
        }
        full = f"{_MOCK_STREAM_TEXT}\n{STREAM_METADATA_DELIMITER}\n{json.dumps(meta)}"
        from app.core.telemetry import get_telemetry

        tele = get_telemetry()

        def _deltas():
            tele.openai.active.inc()
            t0 = time.monotonic()
            try:
                time.sleep(settings.mock_model_latency_ms / 1000.0)
                for i in range(0, len(full), 8):
                    yield full[i:i + 8]
                if usage_out is not None:
                    usage_out["input_tokens"] = 100
                    usage_out["output_tokens"] = 40
                tele.openai.record(latency_ms=(time.monotonic() - t0) * 1000, ok=True)
                tele.openai.record_tokens(100, 40)  # synthetic mock usage
            finally:
                tele.openai.active.dec()

        return _deltas()

    def generate(self, messages: list[dict], usage_out: dict | None = None) -> PatientReply:
        # Patient replies use the patient token limit
        rt = self._runtime()
        limit = rt.patient_max_output_tokens or rt.max_output_tokens
        data = self.generate_structured(
            messages, PATIENT_REPLY_JSON_SCHEMA, "patient_reply",
            max_output_tokens=limit, usage_out=usage_out,
        )
        return PatientReply.model_validate(data)


_default_client: OpenAIPatientClient | None = None


def get_openai_client() -> OpenAIPatientClient:
    global _default_client
    if _default_client is None:
        _default_client = OpenAIPatientClient()
    return _default_client
