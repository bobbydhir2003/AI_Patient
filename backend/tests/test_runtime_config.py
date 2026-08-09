"""Runtime configuration editing: encryption, RBAC, validation, real wiring.

Provider network boundaries are faked (as everywhere in this suite). "A new
request uses the updated value" is proven by checking the value the provider
boundary actually receives after an edit.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import crypto
from app.core.constants import USER_ROLE_ADMIN
from app.core.security import hash_password
from app.database.base import Base
from app.database.connection import get_db, reset_engine
from app.models import ApiCredential, PatientVoiceSetting, User
from app.voice.elevenlabs_client import get_elevenlabs_client
from tests.test_auth import auth_header, login_token
from tests.test_voice import FakeElevenLabsClient


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-encryption-key")
    from app.core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_user(engine, email, role, password="adminpass1"):
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        db.add(User(email=email, password_hash=hash_password(password), full_name="U", role=role, is_active=True))
        db.commit()
    finally:
        db.close()


def _client_with_fakes(engine, elevenlabs=None):
    from app.main import create_app

    app = create_app()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    if elevenlabs is not None:
        app.dependency_overrides[get_elevenlabs_client] = lambda: elevenlabs
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def admins(engine):
    _make_user(engine, "admin@school.edu", USER_ROLE_ADMIN)
    _make_user(engine, "super@school.edu", USER_ROLE_ADMIN)


# --------------------------------------------------------------- encryption
def test_crypto_roundtrip_and_masking():
    tok = crypto.encrypt_secret("sk-proj-SECRET-abcdef123456")
    assert "SECRET" not in tok and tok != "sk-proj-SECRET-abcdef123456"
    assert crypto.decrypt_secret(tok) == "sk-proj-SECRET-abcdef123456"
    assert crypto.mask_secret("sk-proj-SECRET-abcdef123456").endswith("3456")
    assert "SECRET" not in crypto.mask_secret("sk-proj-SECRET-abcdef123456")


# --------------------------------------------------------------- credentials RBAC
def test_replace_key_allowed_for_any_admin(client, engine, admins):
    # Two-role model: every admin may replace credentials (no super-admin tier).
    admin = login_token(client, "admin@school.edu", "adminpass1")
    r = client.post("/api/admin/runtime/credentials/openai",
                    json={"key": "sk-newkey-123456789"}, headers=auth_header(admin))
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_stored_key_is_encrypted_and_never_returned(client, engine, admins):
    su = login_token(client, "super@school.edu", "adminpass1")
    secret = "sk-supersecret-abcdef987654"
    client.post("/api/admin/runtime/credentials/openai", json={"key": secret}, headers=auth_header(su))

    # Encrypted at rest (the raw key is NOT in the stored column).
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    row = db.query(ApiCredential).filter_by(service="openai").one()
    assert secret not in row.encrypted_secret
    assert crypto.decrypt_secret(row.encrypted_secret) == secret  # decryptable server-side
    db.close()

    # The status endpoint returns only a masked value, never the full key.
    data = client.get("/api/admin/runtime/credentials", headers=auth_header(su)).json()
    assert secret not in json.dumps(data)
    assert any("••••" in (c["maskedValue"] or "") for c in data["credentials"])


def test_credential_audit_has_no_raw_secret(client, engine, admins):
    su = login_token(client, "super@school.edu", "adminpass1")
    secret = "sk-audit-check-1234567890"
    client.post("/api/admin/runtime/credentials/elevenlabs", json={"key": secret}, headers=auth_header(su))
    from app.models import AuditLog, ConfigurationHistory
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    blob = " ".join(a.description for a in db.query(AuditLog).all())
    hist = " ".join(h.new_value + h.previous_value for h in db.query(ConfigurationHistory).all())
    db.close()
    assert secret not in blob and secret not in hist


# --------------------------------------------------------------- AI config editing
def test_openai_model_change_persists_and_rejects_unapproved(client, engine, admins):
    admin = login_token(client, "admin@school.edu", "adminpass1")
    ok = client.patch("/api/admin/runtime/ai-configuration/openai",
                      json={"model": "gpt-4o"}, headers=auth_header(admin))
    assert ok.status_code == 200 and ok.json()["applyMode"] == "new_sessions"
    cfg = client.get("/api/admin/runtime/ai-configuration", headers=auth_header(admin)).json()
    assert cfg["openai"]["model"] == "gpt-4o"

    bad = client.patch("/api/admin/runtime/ai-configuration/openai",
                       json={"model": "totally-made-up-model"}, headers=auth_header(admin))
    assert bad.status_code >= 400  # unapproved model rejected


def test_openai_client_uses_runtime_model(monkeypatch, engine, admins):
    """The REAL OpenAI client must send the runtime-selected model on the next
    request (proven at the SDK boundary)."""
    from app.patient_engine.openai_client import OpenAIPatientClient
    from app.services import runtime_config_service as rc

    class _FakeRT:
        api_key = "sk-test"; model = "gpt-4.1"; timeout_seconds = 30.0
        max_output_tokens = 200; patient_max_output_tokens = 200; streaming_enabled = False

    monkeypatch.setattr(OpenAIPatientClient, "_runtime", staticmethod(lambda: _FakeRT()))
    captured = {}

    class _FakeResp:
        output_text = json.dumps({"patient_text": "ok", "used_fact_ids": [], "response_type": "clinical_answer", "supported": True})

    class _FakeSDK:
        class responses:
            @staticmethod
            def create(model, **kw):
                captured["model"] = model
                return _FakeResp()

    c = OpenAIPatientClient()
    monkeypatch.setattr(c, "_get_client", lambda rt=None: _FakeSDK())
    c.generate([{"role": "user", "content": "hi"}])
    assert captured["model"] == "gpt-4.1"  # runtime value used, not the env default


# --------------------------------------------------------------- voices
def test_voice_edit_persists_and_rejects_bad_id(client, engine, admins):
    admin = login_token(client, "admin@school.edu", "adminpass1")
    r = client.patch("/api/admin/runtime/voices/camden/caregiver",
                     json={"voiceId": "MotherVoiceId123", "displayName": "Mom", "stability": 0.6},
                     headers=auth_header(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active" and body["maskedVoiceId"] and "••••" in body["maskedVoiceId"]
    assert "MotherVoiceId123" not in json.dumps(body)  # full id never returned

    bad = client.patch("/api/admin/runtime/voices/carly/patient",
                       json={"voiceId": "bad id"}, headers=auth_header(admin))
    assert bad.status_code >= 400


def test_camden_only_mother_voice_is_configurable(client, engine, admins):
    """Camden exposes a SINGLE voice speaker: the mother (caregiver). The child
    ('patient') speaker no longer exists, so editing it is rejected; only the
    caregiver record can be created."""
    admin = login_token(client, "admin@school.edu", "adminpass1")
    # The Camden child voice speaker is gone -> rejected.
    rejected = client.patch("/api/admin/runtime/voices/camden/patient",
                            json={"voiceId": "CamdenVoice0001"}, headers=auth_header(admin))
    assert rejected.status_code >= 400
    # Only the mother/caregiver voice is configurable.
    ok = client.patch("/api/admin/runtime/voices/camden/caregiver",
                      json={"voiceId": "MotherVoice0002"}, headers=auth_header(admin))
    assert ok.status_code == 200
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    rows = db.query(PatientVoiceSetting).filter_by(case_id="camden").all()
    speakers = {r.speaker_id: r.voice_id for r in rows}
    db.close()
    assert speakers == {"caregiver": "MotherVoice0002"}


def test_preview_does_not_save(client, engine, admins):
    fake = FakeElevenLabsClient()
    c = _client_with_fakes(engine, elevenlabs=fake)
    _make_user(engine, "a2@school.edu", USER_ROLE_ADMIN)
    admin = login_token(c, "a2@school.edu", "adminpass1")
    # Give an elevenlabs key so preview is available.
    _make_user(engine, "s2@school.edu", USER_ROLE_ADMIN)
    su = login_token(c, "s2@school.edu", "adminpass1")
    c.post("/api/admin/runtime/credentials/elevenlabs", json={"key": "sk-el-123456789"}, headers=auth_header(su))

    r = c.post("/api/admin/runtime/voices/sofia/patient/preview",
               json={"voiceId": "UnsavedPreviewVoice", "previewText": "Hi"}, headers=auth_header(admin))
    assert r.status_code == 200 and r.content
    assert fake.calls[0]["voice_id"] == "UnsavedPreviewVoice"  # unsaved value used for audition

    # No override was persisted by previewing.
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    assert db.query(PatientVoiceSetting).filter_by(case_id="sofia", speaker_id="patient").count() == 0
    db.close()


def test_optimistic_locking_blocks_stale_write(client, engine, admins):
    admin = login_token(client, "admin@school.edu", "adminpass1")
    client.patch("/api/admin/runtime/voices/jayden/patient",
                 json={"voiceId": "JaydenVoice0001"}, headers=auth_header(admin))
    r = client.patch("/api/admin/runtime/voices/jayden/patient",
                     json={"voiceId": "JaydenVoice0002", "expectedUpdatedAt": "1999-01-01T00:00:00+00:00"},
                     headers=auth_header(admin))
    assert r.status_code >= 400
    assert "another administrator" in r.text.lower()


# --------------------------------------------------------------- wiring: interview TTS
def test_saved_voice_affects_interview_tts(tmp_path, monkeypatch):
    """load_voice_profile (used by the student interview) must return a saved
    runtime override - proving a dashboard voice edit changes real speech."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'rt.db'}")
    from app.core.config import get_settings
    get_settings.cache_clear()
    reset_engine()
    try:
        from app.database.connection import get_engine, get_session_factory
        Base.metadata.create_all(get_engine())
        db = get_session_factory()()
        from app.services import runtime_config_service as rc
        rc.set_voice(db, case_id="carly", speaker_id="patient",
                     patch={"voice_id": "BrandNewCarlyVoice"}, admin_email="a@x")
        db.commit(); db.close()

        monkeypatch.setattr(get_settings(), "elevenlabs_api_key", "k")
        monkeypatch.setattr(get_settings(), "elevenlabs_enabled", True)
        from app.voice.voice_profile_loader import load_voice_profile
        resolved = load_voice_profile("carly", "patient")
        assert resolved.available and resolved.profile.voice_id == "BrandNewCarlyVoice"
    finally:
        get_settings.cache_clear()
        reset_engine()


# --------------------------------------------------------------- session snapshot
def test_new_session_snapshots_config_and_existing_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'snap.db'}")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "k")
    from app.core.config import get_settings
    get_settings.cache_clear()
    reset_engine()
    try:
        from app.database.connection import get_engine, get_session_factory
        Base.metadata.create_all(get_engine())
        from app.services import session_service, runtime_config_service as rc
        from app.schemas.session_schema import SessionCreateRequest
        from app.models import Student

        # A3: create_session now derives ownership from the authenticated student.
        seed = get_session_factory()()
        st = Student(name="Stu One", student_number="S1", email="s1@x")
        seed.add(st); seed.flush()
        u = User(email="s1@x", password_hash="x", full_name="Stu One",
                 role="student", student_id=st.id, is_active=True)
        seed.add(u); seed.commit(); student_uid = u.id; seed.close()

        db = get_session_factory()()
        first = session_service.create_session(db, SessionCreateRequest(
            studentName="Stu One", studentId="S1", caseId="carly"), db.get(User, student_uid))
        snap1 = json.loads(_snapshot_of(get_session_factory(), first.session_id))
        assert snap1["openai_model"]  # a real model was frozen

        # Admin changes the model AFTER the first interview started.
        db2 = get_session_factory()()
        rc.set_openai_config(db2, admin_email="a@x", patch={"model": "gpt-4o"}); db2.commit(); db2.close()

        db3 = get_session_factory()()
        second = session_service.create_session(db3, SessionCreateRequest(
            studentName="Stu Two", studentId="S2", caseId="carly"), db3.get(User, student_uid))
        snap2 = json.loads(_snapshot_of(get_session_factory(), second.session_id))

        # Existing session keeps its original model; the new one uses the change.
        snap1b = json.loads(_snapshot_of(get_session_factory(), first.session_id))
        assert snap1b["openai_model"] == snap1["openai_model"]
        assert snap2["openai_model"] == "gpt-4o"
    finally:
        get_settings.cache_clear()
        reset_engine()


def _snapshot_of(factory, session_id):
    from app.models import InterviewSession
    db = factory()
    try:
        return db.get(InterviewSession, session_id).config_snapshot
    finally:
        db.close()
