from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.assessment import assessment_service
from app.core.exceptions import AssessmentNotFoundError
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.schemas.admin import (
    DashboardOut,
    DeleteConfirmation,
    MutationResult,
    PaginatedAuditLogs,
    PaginatedSessions,
    PaginatedStudents,
    SearchResultsOut,
    SessionSummaryOut,
    StudentDetailOut,
    StudentStatusUpdate,
    TranscriptMessageOut,
)
from app.schemas.assessment_schema import AssessmentOut
from app.schemas.notification_schema import NotificationListOut
from app.services import admin_service, notification_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---------------- dashboard ----------------
@router.get("/dashboard", response_model=DashboardOut)
def dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    return admin_service.get_dashboard(db)


# ---------------- global search ----------------
@router.get("/search", response_model=SearchResultsOut)
def global_search(
    q: str = Query("", max_length=120),
    limit: int = Query(6, ge=1, le=20),
    db: Session = Depends(get_db),
) -> SearchResultsOut:
    return admin_service.search(db, query=q, limit=limit)


# ---------------- students ----------------
@router.get("/students", response_model=PaginatedStudents)
def list_students(
    search: str = "",
    status: str = Query("all", pattern="^(all|active|inactive)$"),
    sort: str = Query("newest", pattern="^(newest|oldest|name)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedStudents:
    return admin_service.list_students(
        db, search=search, status=status, sort=sort, page=page, page_size=page_size
    )


@router.get("/students/{student_id}", response_model=StudentDetailOut)
def get_student(student_id: str, db: Session = Depends(get_db)) -> StudentDetailOut:
    return admin_service.get_student(db, student_id)


@router.get("/students/{student_id}/sessions", response_model=list[SessionSummaryOut])
def student_sessions(student_id: str, db: Session = Depends(get_db)) -> list[SessionSummaryOut]:
    return admin_service.list_student_sessions(db, student_id)


# ---------------- sessions ----------------
@router.get("/sessions", response_model=PaginatedSessions)
def list_sessions(
    case_id: str = "",
    status: str = "all",
    sort: str = Query("newest", pattern="^(newest|oldest)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedSessions:
    return admin_service.list_sessions(
        db, case_id=case_id, status=status, sort=sort, page=page, page_size=page_size
    )


@router.get("/sessions/{session_id}", response_model=SessionSummaryOut)
def get_session(session_id: str, db: Session = Depends(get_db)) -> SessionSummaryOut:
    return admin_service.get_session_summary(db, session_id)


@router.get("/sessions/{session_id}/transcript", response_model=list[TranscriptMessageOut])
def session_transcript(session_id: str, db: Session = Depends(get_db)) -> list[TranscriptMessageOut]:
    return admin_service.get_session_transcript(db, session_id)


@router.get("/sessions/{session_id}/assessment", response_model=AssessmentOut | None)
def session_assessment(session_id: str, db: Session = Depends(get_db)) -> AssessmentOut | None:
    # Ensure the session exists (404 if not) before probing for an assessment.
    admin_service.get_session_summary(db, session_id)
    try:
        return assessment_service.get_latest_for_session(db, session_id)
    except AssessmentNotFoundError:
        return None


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
def get_assessment(assessment_id: str, db: Session = Depends(get_db)) -> AssessmentOut:
    return assessment_service.get_assessment(db, assessment_id)


# ---------------- audit log ----------------
@router.get("/audit-logs", response_model=PaginatedAuditLogs)
def audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedAuditLogs:
    return admin_service.list_audit_logs(db, page=page, page_size=page_size)


# ---------------- notifications ----------------
@router.get("/notifications", response_model=NotificationListOut)
def notifications(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> NotificationListOut:
    """Real admin notification feed derived from recorded activity."""
    return notification_service.list_notifications(db, admin)


@router.post("/notifications/read-all", response_model=MutationResult)
def read_all_notifications(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    notification_service.mark_all_read(db, admin)
    db.commit()
    return MutationResult(success=True, message="All notifications marked as read.")


# ---------------- mutations ----------------
@router.patch("/students/{student_id}/status", response_model=MutationResult)
def set_student_status(
    student_id: str,
    payload: StudentStatusUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.set_student_status(db, admin, student_id, payload.is_active)
    verb = "reactivated" if payload.is_active else "archived"
    return MutationResult(success=True, message=f"Student {verb}.")


@router.delete("/students/{student_id}", response_model=MutationResult)
def delete_student(
    student_id: str,
    payload: DeleteConfirmation,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.delete_student(db, admin, student_id, confirm=payload.confirm)
    return MutationResult(success=True, message="Student and all connected data deleted.")


@router.patch("/sessions/{session_id}/archive", response_model=MutationResult)
def archive_session(
    session_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.archive_session(db, admin, session_id)
    return MutationResult(success=True, message="Session archived.")


@router.delete("/sessions/{session_id}", response_model=MutationResult)
def delete_session(
    session_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.delete_session(db, admin, session_id)
    return MutationResult(success=True, message="Session deleted.")


@router.delete("/assessments/{assessment_id}", response_model=MutationResult)
def delete_assessment(
    assessment_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.delete_assessment(db, admin, assessment_id)
    return MutationResult(success=True, message="Assessment deleted.")


@router.delete("/messages/{message_id}", response_model=MutationResult)
def delete_message(
    message_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResult:
    admin_service.delete_message(db, admin, message_id)
    return MutationResult(success=True, message="Message deleted.")
