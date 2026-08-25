"""Domain exceptions and FastAPI handlers."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    # Optional extra HTTP headers (e.g. Retry-After on 429). None by default.
    headers: dict | None = None

    def __init__(self, message: str = "Internal server error") -> None:
        super().__init__(message)
        self.message = message


class RateLimitedError(AppError):
    """Too many requests from this identity/IP for a protected action. The
    message never exposes internal provider or infrastructure details."""

    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("Too many requests. Please slow down and try again shortly.")
        if retry_after and retry_after > 0:
            self.headers = {"Retry-After": str(int(retry_after))}


class ServiceOverloadedError(AppError):
    """The server is at its configured AI-interview concurrency limit and could
    not admit this request within the bounded wait. A controlled, safe overload
    signal - never a raw provider error."""

    status_code = 503
    code = "service_overloaded"

    def __init__(self, retry_after: int | None = 5) -> None:
        super().__init__("The system is at capacity right now. Please try again in a moment.")
        if retry_after and retry_after > 0:
            self.headers = {"Retry-After": str(int(retry_after))}


class LoginThrottledError(AppError):
    """Too many failed login attempts for this IP/email pair. Deliberately
    generic so it never reveals whether the email belongs to a real account."""

    status_code = 429
    code = "login_throttled"

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("Too many login attempts. Please wait a moment and try again.")
        if retry_after and retry_after > 0:
            self.headers = {"Retry-After": str(int(retry_after))}


class CaseNotFoundError(AppError):
    status_code = 404
    code = "case_not_found"

    def __init__(self, case_id: str) -> None:
        super().__init__(f"Patient case '{case_id}' was not found.")


class SessionNotFoundError(AppError):
    status_code = 404
    code = "session_not_found"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Interview session '{session_id}' was not found.")


class SessionLockedError(AppError):
    status_code = 409
    code = "session_locked"

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Interview session '{session_id}' is completed and locked; no further messages are allowed."
        )


class TranscriptLockedError(AppError):
    status_code = 409
    code = "transcript_locked"

    def __init__(self) -> None:
        super().__init__("This interview transcript is locked because the session is completed.")


class TranscriptEmptyError(AppError):
    status_code = 409
    code = "transcript_empty"

    def __init__(self) -> None:
        super().__init__(
            "This interview has no usable saved conversation yet. Ask the patient at least "
            "one question before completing the interview."
        )


class CaseSessionMismatchError(AppError):
    status_code = 409
    code = "case_session_mismatch"

    def __init__(self, requested_case_id: str, session_case_id: str) -> None:
        super().__init__(
            f"The request is for case '{requested_case_id}' but the session belongs to case "
            f"'{session_case_id}'. Start a new session for the selected patient."
        )


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_failed"


class PatientEngineError(AppError):
    """Internal engine failure (OpenAI call failed, invalid output, etc.)."""

    status_code = 502
    code = "patient_engine_error"

    def __init__(self, message: str = "The simulated patient could not generate a response.") -> None:
        super().__init__(message)


class StructuredOutputTruncatedError(PatientEngineError):
    """Raised when the OpenAI response was truncated due to output token limits."""
    code = "structured_output_truncated"

    def __init__(self, message: str = "The AI response was truncated before completion.") -> None:
        super().__init__(message)


class PatientResponseUnavailableError(AppError):
    """Returned to the client when generation failed after retries.

    This is deliberate technical error handling - the system must NEVER invent
    a patient reply. The student can retry the question.
    """

    status_code = 503
    code = "PATIENT_RESPONSE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The patient response could not be generated. Please try again.")


class StreamingDisabledError(AppError):
    """The streaming patient-response pipeline is feature-flagged off. The
    frontend treats this as a signal to use the stable non-streaming path."""

    status_code = 409
    code = "streaming_disabled"

    def __init__(self) -> None:
        super().__init__("Streaming patient responses are not enabled on this server.")


class VoiceNotAvailableError(AppError):
    """ElevenLabs voice is disabled, unconfigured, or the case has no voice ID.
    The frontend treats this as a signal to use the browser-TTS fallback."""

    status_code = 409
    code = "voice_unavailable"

    def __init__(self, message: str = "Realistic voice is not available for this patient.") -> None:
        super().__init__(message)


class VoiceSynthesisError(AppError):
    """ElevenLabs call failed (timeout, API error). Message is always safe for
    the frontend - no upstream details, headers, or keys are ever included."""

    status_code = 502
    code = "voice_synthesis_failed"

    def __init__(self) -> None:
        super().__init__("The patient voice could not be generated right now.")


class LiveKitNotConfiguredError(AppError):
    """Phase 1 LiveKit POC only. Raised when LIVEKIT_URL/API_KEY/API_SECRET or
    the livekit_poc_enabled flag are not set - never in the production voice
    path, which does not use LiveKit at all."""

    status_code = 503
    code = "livekit_not_configured"

    def __init__(self) -> None:
        super().__init__("The LiveKit POC is not configured on this server.")


class SessionNotCompletedError(AppError):
    status_code = 409
    code = "session_not_completed"

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Session '{session_id}' must be completed and locked before it can be assessed."
        )


class AssessmentNotPossibleError(AppError):
    status_code = 422
    code = "assessment_not_possible"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AssessmentNotFoundError(AppError):
    status_code = 404
    code = "assessment_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"Assessment '{ref}' was not found.")


class AssessmentUnavailableError(AppError):
    """AI assessment generation failed. No fake feedback is ever produced."""

    status_code = 503
    code = "ASSESSMENT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The AI assessment could not be generated. The transcript is saved - please retry.")


class InvalidCredentialsError(AppError):
    """Generic login failure. The message is deliberately identical whether the
    email is unknown or the password is wrong, so it never reveals which
    accounts exist."""

    status_code = 401
    code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("Incorrect email or password.")


class NotAuthenticatedError(AppError):
    status_code = 401
    code = "not_authenticated"

    def __init__(self, message: str = "Authentication is required.") -> None:
        super().__init__(message)


class InactiveAccountError(AppError):
    status_code = 403
    code = "account_inactive"

    def __init__(self) -> None:
        super().__init__("This account has been deactivated.")


class AccountPendingError(AppError):
    status_code = 403
    code = "account_pending"

    def __init__(self) -> None:
        super().__init__("Your account is still pending administrator approval.")


class AccountRejectedError(AppError):
    status_code = 403
    code = "account_rejected"

    def __init__(self) -> None:
        super().__init__("Your account request was not approved. Please contact the administrator.")


class AccountDisabledError(AppError):
    status_code = 403
    code = "account_disabled"

    def __init__(self) -> None:
        super().__init__("Your account is currently disabled. Please contact the administrator.")


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"

    def __init__(self, message: str = "You do not have permission to perform this action.") -> None:
        super().__init__(message)


class AccessNotApprovedError(AppError):
    status_code = 403
    code = "access_not_approved"

    def __init__(self) -> None:
        super().__init__("Your email has not been approved for access yet.")


class AccessRequestNotFoundError(AppError):
    status_code = 404
    code = "access_request_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"Access request '{ref}' was not found.")


class EmailAlreadyRegisteredError(AppError):
    status_code = 409
    code = "email_already_registered"

    def __init__(self) -> None:
        super().__init__("An account with this email already exists.")


class UserNotFoundError(AppError):
    status_code = 404
    code = "user_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"User '{ref}' was not found.")


class StudentNotFoundError(AppError):
    status_code = 404
    code = "student_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"Student '{ref}' was not found.")


class SelfDeletionError(AppError):
    status_code = 400
    code = "self_deletion_forbidden"

    def __init__(self) -> None:
        super().__init__("You cannot deactivate or delete your own admin account.")


class DeleteConfirmationError(AppError):
    status_code = 400
    code = "delete_confirmation_required"

    def __init__(self) -> None:
        super().__init__("This permanent deletion requires typing DELETE to confirm.")


class LoadTestConflictError(AppError):
    status_code = 409
    code = "load_test_already_running"

    def __init__(self, message: str = "A load test is already running. Stop it before starting another.") -> None:
        super().__init__(message)


class LoadTestNotFoundError(AppError):
    status_code = 404
    code = "load_test_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"Load test '{ref}' was not found.")


class LoadTestConfirmationError(AppError):
    status_code = 422
    code = "load_test_confirmation_required"

    def __init__(self, message: str = "Real-provider load tests spend money and require explicit confirmation.") -> None:
        super().__init__(message)


class LoadTestDisabledError(AppError):
    status_code = 409
    code = "load_test_disabled"

    def __init__(self) -> None:
        super().__init__("Load & capacity testing is disabled by configuration.")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=exc.headers,
        )


class SessionNotCompletedError(AppError):
    status_code = 409
    code = "session_not_completed"

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Session '{session_id}' must be completed and locked before it can be assessed."
        )


class TranscriptTooShortError(AppError):
    status_code = 409
    code = "transcript_too_short"

    def __init__(self) -> None:
        super().__init__("The interview contains no student questions, so it cannot be assessed.")


class AssessmentNotFoundError(AppError):
    status_code = 404
    code = "assessment_not_found"

    def __init__(self, ref: str) -> None:
        super().__init__(f"No assessment was found for '{ref}'.")


class AssessmentUnavailableError(AppError):
    """AI assessment generation failed. No fake feedback is ever shown."""

    status_code = 503
    code = "ASSESSMENT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The AI assessment could not be generated. Please try again.")
