"""AI iEXCEL Course 1: LTI 1.3 tool side (OIDC login, launch, code exchange).

Flow (the platform is the only issuer this tool trusts):

1. login(): the platform's third-party-initiated login. Validates the platform
   parameters, stores a single-use state + nonce, binds the state to this
   browser with an HttpOnly cookie, and redirects to the platform's
   authorization endpoint.
2. launch(): the platform form-posts an RS256 id_token. The state must match
   the cookie and be unused and unexpired; the id_token signature (platform
   JWKS, by kid), issuer, audience/azp, expiry, nonce, message type, version,
   deployment, contract version, role, resource link, and course context are
   all checked. The launch then resolves the AI SP user by the platform's
   opaque subject only - never by email, name, or student number.
3. The browser is redirected to the AI SP web app with a one-time code in the
   URL fragment (never a token, never in a query string). The web app trades
   it once, within 60 s, for a short-lived session token scoped to the course
   offering (exchange()).

Security notes: no state/nonce/code is stored in clear (SHA-256 only, except
the nonce, which is single-use and bound to its state); each is consumed with
a conditional UPDATE so a replay loses the race; reasons are fixed codes and
logs never carry token contents.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.constants import (
    ACCOUNT_STATUS_ACTIVE,
    LTI_ROLE_INSTRUCTOR,
    LTI_ROLE_LEARNER,
    USER_ROLE_STUDENT,
)
from app.core.exceptions import LtiDisabledError, LtiLaunchError
from app.core.logging import get_logger
from app.core.security import create_access_token
from app.models import (
    ContextCaseAssignment,
    ContextMember,
    CourseContext,
    LtiLoginCode,
    LtiLoginState,
    PlatformIdentity,
    Student,
    User,
)

logger = get_logger(__name__)

LTI = "https://purl.imsglobal.org/spec/lti/claim/"
MESSAGE_RESOURCE_LINK = "LtiResourceLinkRequest"
LTI_VERSION = "1.3.0"
CONTRACT_VERSION = "iexcel-integration/1"
ROLE_URIS = {
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor": LTI_ROLE_INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner": LTI_ROLE_LEARNER,
}

STATE_COOKIE = "aisp_lti_state"
STATE_COOKIE_PATH = "/api/lti"
STATE_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(seconds=60)
CLOCK_LEEWAY_SECONDS = 5
_MAX_PARAM = 2048
_PRUNE_AFTER = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def require_enabled() -> Settings:
    """The integration surface exists only when switched on AND configured."""
    settings = get_settings()
    if not settings.lti_ready:
        raise LtiDisabledError()
    return settings


# ------------------------------------------------------------ platform JWKS


class _PlatformKeys:
    """Platform public keys by kid. An unknown kid refetches the JWKS at most
    once per 30 s (key rotation), so a forged kid cannot hammer the platform."""

    _REFETCH_INTERVAL = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._url = ""
        self._fetched_at = 0.0

    def get(self, url: str, kid: str) -> Any:
        with self._lock:
            if url != self._url:
                self._keys, self._url, self._fetched_at = {}, url, 0.0
            if kid not in self._keys and time.monotonic() - self._fetched_at > self._REFETCH_INTERVAL:
                self._fetched_at = time.monotonic()
                try:
                    response = httpx.get(url, timeout=5.0)
                    response.raise_for_status()
                    published = response.json().get("keys", [])
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("lti_jwks_fetch_failed error=%s", type(exc).__name__)
                    published = None
                if published is not None:
                    self._keys = {}
                    for jwk in published:
                        if jwk.get("kid") and jwk.get("kty") == "RSA":
                            self._keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm="RS256").key
            if kid not in self._keys:
                raise LtiLaunchError("unknown_signing_key")
            return self._keys[kid]

    def clear(self) -> None:
        with self._lock:
            self._keys, self._url, self._fetched_at = {}, "", 0.0


platform_keys = _PlatformKeys()


# ------------------------------------------------------------------- login


@dataclass
class LoginRedirect:
    url: str
    state: str


def login(db: Session, params: dict[str, str]) -> LoginRedirect:
    settings = require_enabled()
    if any(len(v) > _MAX_PARAM for v in params.values()):
        raise LtiLaunchError("invalid_login_request")
    if params.get("iss") != settings.lti_platform_issuer:
        raise LtiLaunchError("unknown_platform")
    if params.get("client_id", settings.lti_client_id) != settings.lti_client_id:
        raise LtiLaunchError("unknown_client")
    deployment = params.get("lti_deployment_id")
    if deployment is not None and deployment not in settings.lti_deployment_id_list:
        raise LtiLaunchError("unknown_deployment")
    login_hint = params.get("login_hint", "")
    if not login_hint:
        raise LtiLaunchError("invalid_login_request")
    target = params.get("target_link_uri", "")
    allowed_origins = {_origin(settings.lti_launch_url), _origin(settings.lti_frontend_url)}
    if not target or _origin(target) not in allowed_origins:
        raise LtiLaunchError("foreign_target_link")

    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = _now()
    db.add(LtiLoginState(state_hash=_digest(state), nonce=nonce, expires_at=now + STATE_TTL))
    db.execute(delete(LtiLoginState).where(LtiLoginState.expires_at < now - _PRUNE_AFTER))
    db.execute(delete(LtiLoginCode).where(LtiLoginCode.expires_at < now - _PRUNE_AFTER))
    db.commit()

    query = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": settings.lti_client_id,
        "redirect_uri": settings.lti_launch_url,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if params.get("lti_message_hint"):
        query["lti_message_hint"] = params["lti_message_hint"]
    separator = "&" if urlsplit(settings.lti_authorization_url).query else "?"
    return LoginRedirect(f"{settings.lti_authorization_url}{separator}{urlencode(query)}", state)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


# ------------------------------------------------------------------ launch


@dataclass
class VerifiedLaunch:
    subject: str
    role: str
    context_id: str
    context_label: str
    context_title: str
    deployment_id: str
    name: str | None


def _consume_state(db: Session, state: str, cookie_state: str) -> str:
    """Return the nonce bound to this state, consuming it (single use)."""
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise LtiLaunchError("state_mismatch")
    row = db.execute(
        select(LtiLoginState).where(LtiLoginState.state_hash == _digest(state))
    ).scalar_one_or_none()
    if row is None:
        raise LtiLaunchError("state_mismatch")
    now = _now()
    claimed = db.execute(
        update(LtiLoginState)
        .where(LtiLoginState.id == row.id, LtiLoginState.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    db.commit()
    if claimed != 1:
        raise LtiLaunchError("state_replayed")
    if _as_aware(row.expires_at) <= now:
        raise LtiLaunchError("state_expired")
    return row.nonce


def _as_aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; every stored value is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def verify_id_token(id_token: str, expected_nonce: str) -> VerifiedLaunch:
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise LtiLaunchError("invalid_id_token")
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise LtiLaunchError("invalid_id_token")
    key = platform_keys.get(settings.lti_jwks_url, str(header["kid"]))
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=settings.lti_client_id,
            issuer=settings.lti_platform_issuer,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
        )
    except jwt.ExpiredSignatureError:
        raise LtiLaunchError("id_token_expired")
    except jwt.InvalidAudienceError:
        raise LtiLaunchError("wrong_audience")
    except jwt.InvalidIssuerError:
        raise LtiLaunchError("wrong_issuer")
    except jwt.PyJWTError:
        raise LtiLaunchError("invalid_id_token")

    aud = claims["aud"]
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != settings.lti_client_id:
        raise LtiLaunchError("wrong_audience")
    if claims.get("azp", settings.lti_client_id) != settings.lti_client_id:
        raise LtiLaunchError("wrong_audience")
    if not secrets.compare_digest(str(claims["nonce"]), expected_nonce):
        raise LtiLaunchError("nonce_mismatch")
    if claims.get(LTI + "message_type") != MESSAGE_RESOURCE_LINK:
        raise LtiLaunchError("unsupported_message_type")
    if claims.get(LTI + "version") != LTI_VERSION:
        raise LtiLaunchError("unsupported_lti_version")
    deployment = claims.get(LTI + "deployment_id")
    if deployment not in settings.lti_deployment_id_list:
        raise LtiLaunchError("unknown_deployment")
    custom = claims.get(LTI + "custom") or {}
    if not isinstance(custom, dict) or custom.get("iexcel_contract_version") != CONTRACT_VERSION:
        raise LtiLaunchError("unsupported_contract_version")
    resource_link = claims.get(LTI + "resource_link") or {}
    if not isinstance(resource_link, dict) or not resource_link.get("id"):
        raise LtiLaunchError("missing_resource_link")
    context = claims.get(LTI + "context") or {}
    context_id = context.get("id") if isinstance(context, dict) else None
    if not isinstance(context_id, str) or not 0 < len(context_id) <= 64:
        raise LtiLaunchError("missing_context")
    roles = claims.get(LTI + "roles") or []
    mapped = {ROLE_URIS[r] for r in roles if isinstance(r, str) and r in ROLE_URIS}
    if LTI_ROLE_INSTRUCTOR in mapped:
        role = LTI_ROLE_INSTRUCTOR
    elif LTI_ROLE_LEARNER in mapped:
        role = LTI_ROLE_LEARNER
    else:
        raise LtiLaunchError("unsupported_role")
    subject = claims["sub"]
    if not isinstance(subject, str) or not 0 < len(subject) <= 255:
        raise LtiLaunchError("invalid_subject")
    name = claims.get("name")
    return VerifiedLaunch(
        subject=subject,
        role=role,
        context_id=context_id,
        context_label=str(context.get("label") or "")[:100],
        context_title=str(context.get("title") or "")[:300],
        deployment_id=deployment,
        name=name.strip()[:200] if isinstance(name, str) and name.strip() else None,
    )


def launch(db: Session, form: dict[str, str], cookie_state: str) -> str:
    """Validate a launch and return the one-time code for the web app."""
    settings = require_enabled()
    if form.get("error"):
        raise LtiLaunchError("platform_refused_launch")
    nonce = _consume_state(db, form.get("state", ""), cookie_state)
    verified = verify_id_token(form.get("id_token", ""), nonce)
    _record_context(db, settings, verified)
    user = resolve_user(db, settings.lti_platform_issuer, verified)
    _record_membership(db, verified.context_id, user.id, verified.role)
    code = secrets.token_urlsafe(32)
    db.add(
        LtiLoginCode(
            code_hash=_digest(code),
            user_id=user.id,
            context_id=verified.context_id,
            launch_role=verified.role,
            expires_at=_now() + CODE_TTL,
        )
    )
    db.commit()
    logger.info(
        "lti_launch_accepted user_id=%s context_id=%s role=%s",
        user.id, verified.context_id, verified.role,
    )
    return code


def _record_context(db: Session, settings: Settings, verified: VerifiedLaunch) -> None:
    context = db.get(CourseContext, verified.context_id)
    if context is None:
        db.add(
            CourseContext(
                context_id=verified.context_id,
                issuer=settings.lti_platform_issuer,
                deployment_id=verified.deployment_id,
                label=verified.context_label,
                title=verified.context_title,
            )
        )
        for case_id in settings.lti_default_case_id_list:
            db.add(ContextCaseAssignment(context_id=verified.context_id, case_id=case_id))
        try:
            db.commit()
        except IntegrityError:  # a concurrent first launch created it
            db.rollback()
        return
    if context.issuer != settings.lti_platform_issuer:
        raise LtiLaunchError("context_issuer_mismatch")
    context.deployment_id = verified.deployment_id
    context.label = verified.context_label or context.label
    context.title = verified.context_title or context.title
    db.commit()


def resolve_user(db: Session, issuer: str, verified: VerifiedLaunch) -> User:
    """(issuer, opaque subject) -> the AI SP account. A first launch creates a
    NEW account; an existing AI SP account is never matched by email, name, or
    student number, and is never modified by a launch."""
    identity = db.execute(
        select(PlatformIdentity).where(
            PlatformIdentity.issuer == issuer, PlatformIdentity.subject == verified.subject
        )
    ).scalar_one_or_none()
    if identity is None:
        user = User(
            # Unique, undeliverable placeholder; the platform keeps the real
            # address. Not a login identifier (the account has no password).
            email=f"lti-{_digest(issuer + '|' + verified.subject)[:32]}@lti.invalid",
            full_name=verified.name or "Course participant",
            student_number="",
            password_hash="!",  # never a valid bcrypt hash: direct login impossible
            role=USER_ROLE_STUDENT,
            account_status=ACCOUNT_STATUS_ACTIVE,
            is_active=True,
        )
        db.add(user)
        db.flush()
        identity = PlatformIdentity(issuer=issuer, subject=verified.subject, user_id=user.id)
        db.add(identity)
        try:
            db.flush()
        except IntegrityError:  # a concurrent first launch won; use its account
            db.rollback()
            identity = db.execute(
                select(PlatformIdentity).where(
                    PlatformIdentity.issuer == issuer,
                    PlatformIdentity.subject == verified.subject,
                )
            ).scalar_one()
        logger.info("lti_user_provisioned user_id=%s", identity.user_id)
    user = db.get(User, identity.user_id)
    if user is None or not user.is_active or user.account_status != ACCOUNT_STATUS_ACTIVE:
        raise LtiLaunchError("account_disabled")
    identity.last_launch_at = _now()
    if verified.name and verified.name != user.full_name:
        user.full_name = verified.name
        if user.student is not None:
            user.student.name = verified.name
    if verified.role == LTI_ROLE_LEARNER and user.student_id is None:
        # A learner owns interview sessions through a Student profile. No NUID
        # is known for a launched learner (REDCap survey record ids need one).
        student = Student(name=user.full_name, student_number="", email="")
        db.add(student)
        db.flush()
        user.student_id = student.id
    db.flush()
    return user


def _record_membership(db: Session, context_id: str, user_id: str, role: str) -> None:
    member = db.execute(
        select(ContextMember).where(
            ContextMember.context_id == context_id,
            ContextMember.user_id == user_id,
            ContextMember.role == role,
        )
    ).scalar_one_or_none()
    if member is None:
        db.add(ContextMember(context_id=context_id, user_id=user_id, role=role))
    else:
        member.last_launch_at = _now()
    db.flush()


# ---------------------------------------------------------------- exchange


@dataclass
class ExchangedSession:
    token: str
    expires_in: int
    user: User
    context: CourseContext
    role: str


def exchange(db: Session, code: str) -> ExchangedSession:
    settings = require_enabled()
    if not code or len(code) > 200:
        raise LtiLaunchError("invalid_code")
    row = db.execute(
        select(LtiLoginCode).where(LtiLoginCode.code_hash == _digest(code))
    ).scalar_one_or_none()
    if row is None:
        raise LtiLaunchError("invalid_code")
    now = _now()
    claimed = db.execute(
        update(LtiLoginCode)
        .where(LtiLoginCode.id == row.id, LtiLoginCode.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    db.commit()
    if claimed != 1:
        raise LtiLaunchError("code_replayed")
    if _as_aware(row.expires_at) <= now:
        raise LtiLaunchError("code_expired")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise LtiLaunchError("account_disabled")
    context = db.get(CourseContext, row.context_id)
    token = create_access_token(
        subject=user.id,
        role=user.role,
        expires_minutes=settings.lti_session_minutes,
        lti={"ctx": row.context_id, "role": row.launch_role},
    )
    user.last_login_at = now
    db.commit()
    return ExchangedSession(
        token=token,
        expires_in=settings.lti_session_minutes * 60,
        user=user,
        context=context,
        role=row.launch_role,
    )


# --------------------------------------------------------- case assignment


def assigned_case_ids(db: Session, context_id: str) -> list[str]:
    from app.core.constants import LTI_CASE_IDS

    assigned = set(
        db.execute(
            select(ContextCaseAssignment.case_id).where(
                ContextCaseAssignment.context_id == context_id
            )
        ).scalars()
    )
    return [c for c in LTI_CASE_IDS if c in assigned]  # stable catalog order


def set_assigned_cases(db: Session, context_id: str, case_ids: list[str], by_user_id: str) -> list[str]:
    from app.core.constants import LTI_CASE_IDS
    from app.core.exceptions import ValidationFailedError

    wanted = set(case_ids)
    unknown = wanted - set(LTI_CASE_IDS)
    if unknown:
        raise ValidationFailedError(f"Unknown or unassignable case ids: {sorted(unknown)}")
    current = set(assigned_case_ids(db, context_id))
    if current - wanted:
        db.execute(
            delete(ContextCaseAssignment).where(
                ContextCaseAssignment.context_id == context_id,
                ContextCaseAssignment.case_id.in_(current - wanted),
            )
        )
    for case_id in sorted(wanted - current):
        db.add(ContextCaseAssignment(context_id=context_id, case_id=case_id, assigned_by=by_user_id))
    db.commit()
    logger.info(
        "lti_cases_assigned context_id=%s by=%s cases=%s",
        context_id, by_user_id, ",".join(sorted(wanted)),
    )
    return assigned_case_ids(db, context_id)
