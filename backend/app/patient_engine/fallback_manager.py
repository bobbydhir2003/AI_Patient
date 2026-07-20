"""Failure policy for patient-response generation.

There is intentionally NO canned patient dialogue here. If OpenAI cannot produce
a valid, in-character response after the configured retries, the request fails
with PATIENT_RESPONSE_UNAVAILABLE and the student can retry. The system must
never invent a patient reply or present case facts as spoken dialogue.
"""
from collections.abc import Callable
from typing import TypeVar

from app.core.config import get_settings
from app.core.exceptions import PatientEngineError, PatientResponseUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


def run_with_retry(operation: Callable[[], T], *, what: str = "patient response") -> T:
    """Run `operation`, retrying per settings. Raise PatientResponseUnavailableError on exhaustion."""
    attempts = 1 + max(0, get_settings().openai_max_retries)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except PatientEngineError as exc:
            last_error = exc
            logger.warning("Attempt %d/%d to generate %s failed: %s", attempt, attempts, what, exc)
    logger.error("All %d attempts to generate %s failed: %s", attempts, what, last_error)
    raise PatientResponseUnavailableError() from last_error
