"""AI iEXCEL Course 1 integration endpoints (LTI 1.3 tool).

Every route answers 404 unless AI_SP_LTI_ENABLED is on and configured.

Browser-facing (no AI SP session):
- GET|POST /api/lti/login   OIDC third-party-initiated login from the platform
- POST     /api/lti/launch  id_token form post; continues to the web app with a
                            one-time code in the URL fragment
- POST     /api/lti/session/exchange  one-time code -> course-scoped session token

Course session (token from the exchange):
- GET /api/lti/session, GET /api/lti/cases (learner: assigned cases only)
- /api/lti/instructor/*: roster, sessions, assessment, transcript (when the
  faculty setting allows), case assignment. Always limited to the launched
  offering; never AI SP administrator access.

Development only (ENVIRONMENT=development and AI_SP_DEV_FAKE_AI=true):
- POST /api/dev/sessions/{id}/simulate-interview  seeds a synthetic transcript
  in place of the LiveKit voice interview.
"""
import html
from datetime import datetime
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assessment import assessment_service
from app.core.config import get_settings
from app.core.constants import LTI_ROLE_LEARNER
from app.core.exceptions import (
    AssessmentNotFoundError,
    ForbiddenError,
    LtiDisabledError,
    LtiLaunchError,
    SessionNotFoundError,
    TranscriptLockedError,
)
from app.core.logging import get_logger
from app.database.connection import get_db
from app.dependencies.auth import (
    require_lti_instructor,
    require_lti_session,
    require_session_access,
)
from app.models import ContextMember, CourseContext, InterviewSession, User
from app.schemas.admin import SessionSummaryOut, TranscriptMessageOut
from app.schemas.assessment_schema import AssessmentOut
from app.schemas.auth import UserOut
from app.schemas.base import CamelModel
from app.schemas.case_schema import CaseSummary
from app.services import admin_service, case_service, lti_service

logger = get_logger(__name__)

router = APIRouter(prefix="/lti", tags=["lti"])
dev_router = APIRouter(prefix="/dev", tags=["development"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


# ------------------------------------------------------------------ schemas


class LtiContextOut(CamelModel):
    id: str
    label: str
    title: str


class LtiSessionOut(CamelModel):
    role: str  # learner | instructor
    context: LtiContextOut
    user: UserOut
    assigned_case_ids: list[str]
    dev_fake_ai: bool = False  # development stand-in active (never in production)


class LtiExchangeIn(CamelModel):
    code: str = Field(min_length=1, max_length=200)


class LtiTokenOut(CamelModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    session: LtiSessionOut


class RosterMemberOut(CamelModel):
    user_id: str
    full_name: str
    first_launch_at: datetime
    last_launch_at: datetime
    session_count: int
    completed_count: int


class CaseAssignmentIn(CamelModel):
    case_ids: list[str] = Field(max_length=10)


class CaseAssignmentOut(CamelModel):
    assigned_case_ids: list[str]
    assignable_cases: list[CaseSummary]


class InstructorSessionDetailOut(CamelModel):
    session: SessionSummaryOut
    assessment: AssessmentOut | None
    transcripts_enabled: bool


class SimulatedInterviewOut(CamelModel):
    session_id: str
    turns_written: int


# ------------------------------------------------------------- launch flow


def _error_url(reason: str) -> str:
    frontend = get_settings().lti_frontend_url.rstrip("/")
    if not frontend:
        raise LtiDisabledError()
    return f"{frontend}/lti/error?reason={quote(reason)}"


def _error_redirect(reason: str) -> RedirectResponse:
    return RedirectResponse(_error_url(reason), status_code=303, headers=_NO_STORE)


def _navigate_page(url: str) -> HTMLResponse:
    """Continue to the web app from the launch POST.

    A 303 is not used here: browsers enforce the platform form-post page's CSP
    form-action on redirects that follow the form submission, and the web app
    is a different origin from this API. A script-free meta refresh starts a
    new navigation instead. The page itself runs nothing and loads nothing."""
    target = html.escape(url, quote=True)
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={target}">'
        "<title>Opening your course</title></head><body>"
        f'<p><a href="{target}">Continue to your course</a></p></body></html>'
    )
    return HTMLResponse(
        page,
        headers={
            **_NO_STORE,
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        },
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body())[:65536]
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


@router.api_route("/login", methods=["GET", "POST"], include_in_schema=False)
async def lti_login(request: Request, db: Session = Depends(get_db)):
    settings = lti_service.require_enabled()
    params = dict(request.query_params)
    if request.method == "POST":
        params.update(await _form(request))
    try:
        result = lti_service.login(db, params)
    except LtiLaunchError as exc:
        logger.warning("lti_login_rejected reason=%s", exc.reason)
        return _error_redirect(exc.reason)
    response = RedirectResponse(result.url, status_code=302, headers=_NO_STORE)
    response.set_cookie(
        lti_service.STATE_COOKIE,
        result.state,
        max_age=int(lti_service.STATE_TTL.total_seconds()),
        path=lti_service.STATE_COOKIE_PATH,
        httponly=True,
        secure=settings.lti_cookie_secure,
        # The launch arrives as a cross-site form POST from the platform.
        samesite="none" if settings.lti_cookie_secure else "lax",
    )
    return response


@router.post("/launch", include_in_schema=False)
async def lti_launch(request: Request, db: Session = Depends(get_db)):
    settings = lti_service.require_enabled()
    form = await _form(request)
    cookie_state = request.cookies.get(lti_service.STATE_COOKIE, "")
    try:
        code = lti_service.launch(db, form, cookie_state)
    except LtiLaunchError as exc:
        db.rollback()
        logger.warning("lti_launch_rejected reason=%s", exc.reason)
        response = _navigate_page(_error_url(exc.reason))
    else:
        frontend = settings.lti_frontend_url.rstrip("/")
        # A fragment never reaches a server or a Referer header.
        response = _navigate_page(f"{frontend}/lti/callback#code={quote(code)}")
    response.delete_cookie(
        lti_service.STATE_COOKIE,
        path=lti_service.STATE_COOKIE_PATH,
        secure=settings.lti_cookie_secure,
        httponly=True,
        samesite="none" if settings.lti_cookie_secure else "lax",
    )
    return response


def _session_out(db: Session, user: User, context: CourseContext | None, role: str, context_id: str) -> LtiSessionOut:
    return LtiSessionOut(
        role=role,
        context=LtiContextOut(
            id=context_id,
            label=context.label if context else "",
            title=context.title if context else "",
        ),
        user=UserOut.model_validate(user),
        assigned_case_ids=lti_service.assigned_case_ids(db, context_id),
        dev_fake_ai=get_settings().dev_fake_ai_active,
    )


@router.post("/session/exchange", response_model=LtiTokenOut)
def exchange_code(payload: LtiExchangeIn, db: Session = Depends(get_db)) -> LtiTokenOut:
    result = lti_service.exchange(db, payload.code)
    return LtiTokenOut(
        access_token=result.token,
        expires_in=result.expires_in,
        session=_session_out(db, result.user, result.context, result.role, result.context.context_id),
    )


@router.get("/session", response_model=LtiSessionOut)
def current_session(
    current_user: User = Depends(require_lti_session), db: Session = Depends(get_db)
) -> LtiSessionOut:
    lti_service.require_enabled()
    ctx = current_user.lti_claims["ctx"]
    return _session_out(db, current_user, db.get(CourseContext, ctx), current_user.lti_claims["role"], ctx)


@router.get("/cases", response_model=list[CaseSummary])
def assigned_cases(
    current_user: User = Depends(require_lti_session), db: Session = Depends(get_db)
) -> list[CaseSummary]:
    lti_service.require_enabled()
    assigned = lti_service.assigned_case_ids(db, current_user.lti_claims["ctx"])
    return [case_service.get_case(case_id) for case_id in assigned]


# --------------------------------------------------------------- instructor


def _context_session(db: Session, ctx: str, session_id: str) -> InterviewSession:
    session = db.get(InterviewSession, session_id)
    if session is None or session.context_id != ctx:
        raise SessionNotFoundError(session_id)  # never reveals other courses' sessions
    return session


@router.get("/instructor/roster", response_model=list[RosterMemberOut])
def instructor_roster(
    current_user: User = Depends(require_lti_instructor), db: Session = Depends(get_db)
) -> list[RosterMemberOut]:
    lti_service.require_enabled()
    ctx = current_user.lti_claims["ctx"]
    members = db.execute(
        select(ContextMember, User)
        .join(User, User.id == ContextMember.user_id)
        .where(ContextMember.context_id == ctx, ContextMember.role == LTI_ROLE_LEARNER)
        .order_by(User.full_name)
    ).all()
    counts = {
        (student_id, status): n
        for student_id, status, n in db.execute(
            select(InterviewSession.student_id, InterviewSession.status, func.count())
            .where(InterviewSession.context_id == ctx)
            .group_by(InterviewSession.student_id, InterviewSession.status)
        ).all()
    }
    out = []
    for member, user in members:
        mine = {s: n for (sid, s), n in counts.items() if sid == user.student_id}
        out.append(
            RosterMemberOut(
                user_id=user.id,
                full_name=user.full_name,
                first_launch_at=member.first_launch_at,
                last_launch_at=member.last_launch_at,
                session_count=sum(mine.values()),
                completed_count=mine.get("completed", 0),
            )
        )
    return out


@router.get("/instructor/sessions", response_model=list[SessionSummaryOut])
def instructor_sessions(
    current_user: User = Depends(require_lti_instructor), db: Session = Depends(get_db)
) -> list[SessionSummaryOut]:
    lti_service.require_enabled()
    rows = db.execute(
        select(InterviewSession)
        .where(InterviewSession.context_id == current_user.lti_claims["ctx"])
        .order_by(InterviewSession.started_at.desc())
        .limit(500)
    ).scalars().all()
    return [admin_service.session_summary(db, s) for s in rows]


@router.get("/instructor/sessions/{session_id}", response_model=InstructorSessionDetailOut)
def instructor_session(
    session_id: str,
    current_user: User = Depends(require_lti_instructor),
    db: Session = Depends(get_db),
) -> InstructorSessionDetailOut:
    lti_service.require_enabled()
    session = _context_session(db, current_user.lti_claims["ctx"], session_id)
    try:
        assessment = assessment_service.get_latest_for_session(db, session.id)
    except AssessmentNotFoundError:
        assessment = None
    return InstructorSessionDetailOut(
        session=admin_service.session_summary(db, session),
        assessment=assessment,
        transcripts_enabled=get_settings().lti_instructor_transcripts_enabled,
    )


@router.get("/instructor/sessions/{session_id}/transcript", response_model=list[TranscriptMessageOut])
def instructor_transcript(
    session_id: str,
    current_user: User = Depends(require_lti_instructor),
    db: Session = Depends(get_db),
) -> list[TranscriptMessageOut]:
    lti_service.require_enabled()
    session = _context_session(db, current_user.lti_claims["ctx"], session_id)
    if not get_settings().lti_instructor_transcripts_enabled:
        raise ForbiddenError("Transcript access for course instructors is not enabled.")
    logger.info(
        "lti_instructor_transcript_viewed user_id=%s session_id=%s", current_user.id, session.id
    )
    return admin_service.get_session_transcript(db, session.id)


def _assignment_out(db: Session, ctx: str) -> CaseAssignmentOut:
    from app.core.constants import LTI_CASE_IDS

    return CaseAssignmentOut(
        assigned_case_ids=lti_service.assigned_case_ids(db, ctx),
        assignable_cases=[case_service.get_case(c) for c in LTI_CASE_IDS],
    )


@router.get("/instructor/cases", response_model=CaseAssignmentOut)
def instructor_cases(
    current_user: User = Depends(require_lti_instructor), db: Session = Depends(get_db)
) -> CaseAssignmentOut:
    lti_service.require_enabled()
    return _assignment_out(db, current_user.lti_claims["ctx"])


@router.put("/instructor/cases", response_model=CaseAssignmentOut)
def assign_cases(
    payload: CaseAssignmentIn,
    current_user: User = Depends(require_lti_instructor),
    db: Session = Depends(get_db),
) -> CaseAssignmentOut:
    lti_service.require_enabled()
    ctx = current_user.lti_claims["ctx"]
    lti_service.set_assigned_cases(db, ctx, payload.case_ids, current_user.id)
    return _assignment_out(db, ctx)


# -------------------------------------------------------------- development


@dev_router.post("/sessions/{session_id}/simulate-interview", response_model=SimulatedInterviewOut)
def simulate_interview(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SimulatedInterviewOut:
    """Stand-in for the LiveKit voice interview on a development machine:
    writes a synthetic student/patient transcript through the same repository
    the voice worker uses. The session must still be completed normally."""
    if not get_settings().dev_fake_ai_active:
        raise LtiDisabledError()
    if session.locked:
        raise TranscriptLockedError()
    from app.patient_engine.dev_fake_ai import SIMULATED_EXCHANGE
    from app.repositories.transcript_repository import TranscriptRepository

    repo = TranscriptRepository(db)
    written = 0
    for i, (student_text, patient_text) in enumerate(SIMULATED_EXCHANGE, start=1):
        if repo.get_by_client_turn_id(session.id, f"dev-s{i}") is None:
            repo.append_turn(session.id, "student", student_text, client_turn_id=f"dev-s{i}", source="typed")
            written += 1
        if repo.get_by_client_turn_id(session.id, f"dev-p{i}") is None:
            repo.append_turn(
                session.id, "patient", patient_text, client_turn_id=f"dev-p{i}", source="openai_realtime"
            )
            written += 1
    db.commit()
    return SimulatedInterviewOut(session_id=session.id, turns_written=written)
