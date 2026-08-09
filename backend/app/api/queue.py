"""Interview waiting-queue endpoints (authenticated students/admins).

Used by the "Interview Queue / System Busy" screen. Every value is real queue
state (see services/interview_queue.py). Leaving the queue only removes the
waiting entry — it never affects sessions, transcripts, assessments or grading.
"""
from fastapi import APIRouter, Depends

from app.database.connection import get_db
from app.dependencies.auth import get_current_user
from app.models import User
from app.schemas.base import CamelModel
from app.services import interview_queue

router = APIRouter(prefix="/queue", tags=["queue"])


class QueueJoinRequest(CamelModel):
    case_id: str


def _holder(user: User) -> str:
    # A student holds by their Student profile id; a practicing admin holds by
    # their user id. Either way, one queue entry per person.
    return user.student_id or user.id


@router.post("/join")
def join_queue(payload: QueueJoinRequest, user: User = Depends(get_current_user), db=Depends(get_db)) -> dict:
    return interview_queue.join(db, _holder(user), payload.case_id)


@router.get("/status/{entry_id}")
def queue_status(entry_id: str, user: User = Depends(get_current_user), db=Depends(get_db)) -> dict:
    return interview_queue.status(db, entry_id)


@router.post("/leave/{entry_id}")
def leave_queue(entry_id: str, user: User = Depends(get_current_user), db=Depends(get_db)) -> dict:
    return interview_queue.leave(db, entry_id, _holder(user))
