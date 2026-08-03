from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import get_current_user
from app.models import User
from app.core.rate_limit import client_ip, rate_limit
from app.schemas.auth import (
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
    RegisterResult,
    TokenResponse,
    UserOut,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

_login_rate_limit = rate_limit("login", lambda s: s.login_rate_limit)
_register_rate_limit = rate_limit("register", lambda s: s.login_rate_limit)


@router.post(
    "/register",
    response_model=RegisterResult,
    status_code=201,
    dependencies=[Depends(_register_rate_limit)],
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResult:
    # D2: creates a PENDING account and returns a status message (no token/auto-login).
    return auth_service.register(db, payload)


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(_login_rate_limit)])
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    return auth_service.login(db, payload.email, payload.password, client_ip=client_ip(request))


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return auth_service.to_user_out(current_user)


@router.post("/logout", response_model=LogoutResponse)
def logout(current_user: User = Depends(get_current_user)) -> LogoutResponse:
    """Stateless JWT logout: the client discards the token. This endpoint
    confirms the token was valid and exists for symmetry / future revocation."""
    return LogoutResponse(success=True)
