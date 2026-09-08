"""Editable runtime configuration endpoints.

Access (enforced here, not just in the UI): every route requires a normal admin
(`require_admin`). There is no separate super/system-admin tier — all admins may
view/edit AI settings, run connection tests, and replace/remove API keys and
restore configuration versions.

Patient interview voice is provided by OpenAI Realtime (see app/livekit_agent/
and app/livekit_agent/realtime_patient_configs.py); there is no editable
ElevenLabs/patient-voice configuration here anymore.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.constants import (
    AUDIT_AI_CONFIG_UPDATED,
    AUDIT_CREDENTIAL_REMOVED,
    AUDIT_CREDENTIAL_REPLACED,
    AUDIT_CREDENTIAL_TESTED,
)
from app.core.logging import get_logger
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.repositories.audit_repository import AuditRepository
from app.schemas.runtime_schema import (
    ApplyResult,
    CredentialListOut,
    CredentialReplaceIn,
    CredentialStatusOut,
    HistoryListOut,
    OpenAIConfigPatchIn,
    TestResultOut,
)
from app.services import runtime_config_service as rc

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/runtime", tags=["admin-runtime"])


def _audit(db, admin, action, record_type, record_id, description):
    AuditRepository(db).record(
        admin_user_id=admin.id, admin_email=admin.email, action_type=action,
        record_type=record_type, record_id=record_id, description=description,
    )


# ------------------------------ credentials (admin) ---------------------------
@router.get("/credentials", response_model=CredentialListOut, dependencies=[Depends(require_admin)])
def get_credentials(db: Session = Depends(get_db)) -> CredentialListOut:
    return CredentialListOut(
        credentials=[CredentialStatusOut.model_validate(c) for c in rc.credential_status(db)]
    )


def _replace_credential(service, payload, admin, db) -> ApplyResult:
    rc.set_credential(db, service=service, new_key=payload.key, admin_email=admin.email)
    _audit(db, admin, AUDIT_CREDENTIAL_REPLACED, "credential", service,
           f"Replaced {service} API key")
    db.commit()
    return ApplyResult(success=True, apply_mode="immediate",
                       message=f"{service} key stored securely. Applies to the next request.")


@router.post("/credentials/openai", response_model=ApplyResult)
def replace_openai_key(payload: CredentialReplaceIn, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)) -> ApplyResult:
    return _replace_credential("openai", payload, admin, db)


@router.delete("/credentials/{service}", response_model=ApplyResult)
def remove_key(service: str, admin: User = Depends(require_admin),
               db: Session = Depends(get_db)) -> ApplyResult:
    rc.remove_credential(db, service=service, admin_email=admin.email)
    _audit(db, admin, AUDIT_CREDENTIAL_REMOVED, "credential", service, f"Removed {service} API key")
    db.commit()
    return ApplyResult(success=True, apply_mode="immediate", message=f"{service} key removed.")


def _test_credential(service, admin, db) -> TestResultOut:
    creds = {c["service"]: c for c in rc.credential_status(db)}
    if not creds.get(service, {}).get("configured"):
        return TestResultOut(service=service, status="not_configured", message="No key configured.")
    # Decrypt/resolve the ACTIVE key (never returned to the client) and test it.
    key = rc._active_key(db, "openai", "")
    status, msg = rc.test_openai_key(key)
    rc.record_test_result(db, service=service, status=status, message=msg)
    _audit(db, admin, AUDIT_CREDENTIAL_TESTED, "credential", service,
           f"Tested {service} connection: {status}")
    db.commit()
    return TestResultOut(service=service, status=status, message=msg)


@router.post("/credentials/openai/test", response_model=TestResultOut, dependencies=[Depends(require_admin)])
def test_openai(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> TestResultOut:
    return _test_credential("openai", admin, db)


# ------------------------------ AI configuration (admin) ----------------------
@router.get("/ai-configuration", dependencies=[Depends(require_admin)])
def get_ai_config(db: Session = Depends(get_db)) -> dict:
    return rc.ai_configuration(db)


@router.patch("/ai-configuration/openai", response_model=ApplyResult)
def patch_openai(payload: OpenAIConfigPatchIn, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> ApplyResult:
    patch = payload.model_dump(exclude_none=True)
    rc.set_openai_config(db, admin_email=admin.email, patch=patch)
    _audit(db, admin, AUDIT_AI_CONFIG_UPDATED, "openai_config", "openai",
           f"Updated OpenAI config: {', '.join(patch.keys())}")
    db.commit()
    mode = "new_sessions" if ("model" in patch or "max_output_tokens" in patch) else "immediate"
    return ApplyResult(success=True, apply_mode=mode,
                       message="Saved. Model/token changes apply to new interview sessions.")


# ------------------------------ history (admin) -------------------------------
@router.get("/history", response_model=HistoryListOut, dependencies=[Depends(require_admin)])
def get_history(db: Session = Depends(get_db)) -> HistoryListOut:
    from app.schemas.runtime_schema import HistoryItemOut
    return HistoryListOut(history=[HistoryItemOut.model_validate(h) for h in rc.list_history(db)])
