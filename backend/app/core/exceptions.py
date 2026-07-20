"""Domain exceptions and FastAPI handlers."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "Internal server error") -> None:
        super().__init__(message)
        self.message = message


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


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"

    def __init__(self, message: str = "You do not have permission to perform this action.") -> None:
        super().__init__(message)


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
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
