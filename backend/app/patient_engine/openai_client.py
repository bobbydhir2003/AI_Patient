"""Thin wrapper around the OpenAI Python SDK (Responses API, structured output)."""
import json

from app.core.exceptions import PatientEngineError, StructuredOutputTruncatedError
from app.core.logging import get_logger
from app.schemas.interview_schema import PATIENT_REPLY_JSON_SCHEMA, PatientReply

logger = get_logger(__name__)


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

            self._client = OpenAI(api_key=rt.api_key, timeout=rt.timeout_seconds)
            self._client_fingerprint = fingerprint
        return self._client

    def _do_generate(self, messages: list[dict], schema: dict, schema_name: str, resolved_tokens: int) -> dict:
        rt = self._runtime()
        client = self._get_client(rt)
        response = client.responses.create(
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
        )
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

    def generate_structured(
        self,
        messages: list[dict],
        schema: dict,
        schema_name: str,
        max_output_tokens: int | None = None,
        allow_truncation_retry: bool = False,
    ) -> dict:
        """Generic structured-output call (Responses API, strict JSON schema)."""
        rt = self._runtime()
        if not rt.api_key:
            raise PatientEngineError("OPENAI_API_KEY is not configured.")

        resolved_tokens = max_output_tokens or rt.max_output_tokens
        try:
            return self._do_generate(messages, schema, schema_name, resolved_tokens)
        except StructuredOutputTruncatedError:
            if allow_truncation_retry:
                retry_tokens = resolved_tokens + 1000
                logger.warning(
                    "truncation_retry_triggered task=%s initial_tokens=%s new_tokens=%s", 
                    schema_name, resolved_tokens, retry_tokens
                )
                return self._do_generate(messages, schema, schema_name, retry_tokens)
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
        rt = self._runtime()
        if not rt.api_key:
            raise PatientEngineError("OPENAI_API_KEY is not configured.")
        resolved_tokens = (
            max_output_tokens
            or rt.patient_max_output_tokens
            or rt.max_output_tokens
        )
        client = self._get_client(rt)
        try:
            stream = client.responses.create(
                model=rt.model,
                input=messages,
                max_output_tokens=resolved_tokens,
                stream=True,
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
                        if usage_out is not None and usage is not None:
                            usage_out["input_tokens"] = getattr(usage, "input_tokens", None)
                            usage_out["output_tokens"] = getattr(usage, "output_tokens", None)
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

    def generate(self, messages: list[dict]) -> PatientReply:
        # Patient replies use the patient token limit
        rt = self._runtime()
        limit = rt.patient_max_output_tokens or rt.max_output_tokens
        data = self.generate_structured(messages, PATIENT_REPLY_JSON_SCHEMA, "patient_reply", max_output_tokens=limit)
        return PatientReply.model_validate(data)


_default_client: OpenAIPatientClient | None = None


def get_openai_client() -> OpenAIPatientClient:
    global _default_client
    if _default_client is None:
        _default_client = OpenAIPatientClient()
    return _default_client
