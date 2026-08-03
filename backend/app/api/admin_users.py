"""Admin account management API (D3).

Operates on USER accounts (approval lifecycle + roles), distinct from the
student-record management in admin.py. Every route requires an admin; the finer
permission rules (super-admin-only role changes, last-super-admin protection,
no self-role-change) are enforced in user_admin_service. There is deliberately
NO public role-change endpoint.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.schemas.auth import ReviewNoteIn, RoleChangeIn, UserOut
from app.services import user_admin_service

router = APIRouter(prefix="/admin/users", tags=["admin-users"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[UserOut])
def list_users(
    status: str | None = Query(default=None),
    role: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in user_admin_service.list_users(db, status=status, role=role)]


@router.post("/{user_id}/approve", response_model=UserOut)
def approve(user_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(user_admin_service.approve(db, admin, user_id))


@router.post("/{user_id}/reject", response_model=UserOut)
def reject(user_id: str, payload: ReviewNoteIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(user_admin_service.reject(db, admin, user_id, payload.note))


@router.post("/{user_id}/disable", response_model=UserOut)
def disable(user_id: str, payload: ReviewNoteIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(user_admin_service.disable(db, admin, user_id, payload.note))


@router.post("/{user_id}/enable", response_model=UserOut)
def enable(user_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(user_admin_service.enable(db, admin, user_id))


@router.post("/{user_id}/role", response_model=UserOut)
def change_role(user_id: str, payload: RoleChangeIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    return UserOut.model_validate(user_admin_service.change_role(db, admin, user_id, payload.role))
