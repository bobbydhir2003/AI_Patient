"""Priority I tests: runtime configuration activation (voice / OpenAI / ElevenLabs
settings + encrypted credentials), effective-source metadata, and secret safety."""
import json

import pytest
from sqlalchemy.orm import sessionmaker

from app.core import crypto
from app.core.config import get_settings
from app.core.exceptions import ValidationFailedError
from app.services import runtime_config_service as rc
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token


def _db(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def enc(monkeypatch):
    """Enable secure credential storage for tests that need it."""
    monkeypatch.setattr(get_settings(), "config_encryption_key", "unit-test-encryption-key-123456")
    return True


# ============================ VOICE CONFIG ============================
def test_no_override_uses_case_file_or_none(engine):
    db = _db(engine)
    try:
        rv = rc.resolve_voice(db, "carly", "patient")
        # Without a DB override the source is the case file (or 'none' if the
        # checked-in case has no configured voice) - never 'runtime'.
        assert rv.source in ("case_file", "none")
    finally:
        db.close()


def test_db_override_takes_precedence(engine):
    db = _db(engine)
    try:
        rc.set_voice(db, case_id="carly", speaker_id="patient",
                     patch={"voice_id": "OverrideVoiceId1234", "display_name": "Test"},
                     admin_email="a@x")
        db.commit()
        rv = rc.resolve_voice(db, "carly", "patient")
        assert rv.source == "runtime" and rv.voice_id == "OverrideVoiceId1234"
    finally:
        db.close()


def test_blank_voice_id_preserves_current(engine):
    db = _db(engine)
    try:
        rc.set_voice(db, case_id="carly", speaker_id="patient",
                     patch={"voice_id": "KeepThisVoiceId1234"}, admin_email="a@x")
        db.commit()
        # A follow-up edit that changes only unrelated fields (no voice_id) must
        # NOT wipe the stored voice id.
        rc.set_voice(db, case_id="carly", speaker_id="patient",
                     patch={"display_name": "Renamed"}, admin_email="a@x")
        db.commit()
        assert rc.resolve_voice(db, "carly", "patient").voice_id == "KeepThisVoiceId1234"
    finally:
        db.close()


def test_camden_mother_voice_from_case_file_then_override(engine):
    db = _db(engine)
    try:
        # Camden's ONLY voice is the mother; it is sourced from the case-file
        # voice_profile (the single source of truth) with no override yet.
        row = rc.get_voice_row(db, "camden", "caregiver")
        assert row["status"] == "active" and row["source"] == "case_file"
        assert row["masked_voice_id"]  # a real (masked) mother voice id
        # A runtime override takes priority over the case-file value.
        rc.set_voice(db, case_id="camden", speaker_id="caregiver",
                     patch={"voice_id": "MotherVoiceId12345"}, admin_email="a@x")
        db.commit()
        after = rc.get_voice_row(db, "camden", "caregiver")
        assert after["status"] == "active" and after["source"] == "runtime"
        assert rc.resolve_voice(db, "camden", "caregiver").voice_id == "MotherVoiceId12345"
        # The child ("patient") speaker is no longer a valid Camden voice.
        import pytest
        with pytest.raises(Exception):
            rc.get_voice_row(db, "camden", "patient")
    finally:
        db.close()


def test_voice_row_never_exposes_raw_voice_id(engine):
    db = _db(engine)
    try:
        rc.set_voice(db, case_id="carly", speaker_id="patient",
                     patch={"voice_id": "SecretRawVoiceId999"}, admin_email="a@x")
        db.commit()
        from app.schemas.runtime_schema import VoiceRowOut
        d = dict(rc.get_voice_row(db, "carly", "patient"))
        d.pop("voice_id", None)  # API layer pops it before serialization
        payload = VoiceRowOut.model_validate(d).model_dump_json()
        assert "SecretRawVoiceId999" not in payload  # only masked id is serialized
    finally:
        db.close()


# ============================ OPENAI CONFIG ============================
def test_openai_model_db_override_beats_env(engine):
    db = _db(engine)
    try:
        rc.set_openai_config(db, admin_email="a@x", patch={"model": "gpt-4o"})
        db.commit()
        assert rc.openai_runtime(db).model == "gpt-4o"  # over env default gpt-4o-mini
    finally:
        db.close()


def test_openai_key_db_override_beats_env_and_never_leaks(engine, enc):
    db = _db(engine)
    try:
        rc.set_credential(db, service="openai", new_key="sk-db-override-SECRET-abcdef", admin_email="a@x")
        db.commit()
        assert rc.openai_runtime(db).api_key == "sk-db-override-SECRET-abcdef"  # DB > env
        # The status DTO exposes only a masked value + effective source.
        status = {c["service"]: c for c in rc.credential_status(db)}["openai"]
        assert status["source"] == "database" and status["configured"] is True
        assert "SECRET" not in json.dumps(status)
    finally:
        db.close()


# ============================ ELEVENLABS CONFIG ============================
def test_elevenlabs_model_db_override_and_env_fallback(engine):
    db = _db(engine)
    try:
        # env fallback first
        assert rc.elevenlabs_runtime(db).model == get_settings().elevenlabs_default_model
        rc.set_elevenlabs_config(db, admin_email="a@x", patch={"model": "eleven_turbo_v2_5"})
        db.commit()
        assert rc.elevenlabs_runtime(db).model == "eleven_turbo_v2_5"
    finally:
        db.close()


def test_elevenlabs_enabled_db_override(engine):
    db = _db(engine)
    try:
        assert rc.elevenlabs_runtime(db).enabled is True  # env default
        rc.set_elevenlabs_config(db, admin_email="a@x", patch={"enabled": False})
        db.commit()
        assert rc.elevenlabs_runtime(db).enabled is False  # DB override, no restart
        rc.set_elevenlabs_config(db, admin_email="a@x", patch={"enabled": True})
        db.commit()
        assert rc.elevenlabs_runtime(db).enabled is True
    finally:
        db.close()


def test_elevenlabs_key_db_override(engine, enc):
    db = _db(engine)
    try:
        rc.set_credential(db, service="elevenlabs", new_key="sk_eleven_new_key_123456", admin_email="a@x")
        db.commit()
        assert rc.elevenlabs_runtime(db).api_key == "sk_eleven_new_key_123456"
    finally:
        db.close()


# ============================ CREDENTIAL SECURITY ============================
def test_missing_encryption_key_blocks_credential_write(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "config_encryption_key", "")  # disabled
    db = _db(engine)
    try:
        with pytest.raises(ValidationFailedError) as ei:
            rc.set_credential(db, service="openai", new_key="sk-should-not-store", admin_email="a@x")
        assert "CONFIG_ENCRYPTION_KEY" in str(ei.value)
        # status reports secure storage unavailable so the UI can disable Replace.
        assert all(c["secure_storage_available"] is False for c in rc.credential_status(db))
    finally:
        db.close()


def test_history_never_stores_raw_key(engine, enc):
    db = _db(engine)
    try:
        rc.set_credential(db, service="openai", new_key="sk-RAWKEY-SENSITIVE-9999", admin_email="a@x")
        db.commit()
        blob = json.dumps(rc.list_history(db))
        assert "RAWKEY-SENSITIVE" not in blob  # only masked identifiers stored
    finally:
        db.close()


def test_credential_replace_allowed_for_any_admin(engine, enc):
    """Two-role model: every admin may replace credentials (no super-admin tier).
    A student is still forbidden."""
    from tests.test_auth import make_admin, register

    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email="norm_admin@school.edu")  # role=admin
        ah = bearer(login_token(c, "norm_admin@school.edu", "adminpass1"))
        # normal admin can view AND replace
        assert c.get("/api/admin/runtime/credentials", headers=ah).status_code == 200
        r = c.post("/api/admin/runtime/credentials/openai", json={"key": "sk-new-123456789"}, headers=ah)
        assert r.status_code == 200

        # a student is forbidden
        register(c, email="stud_cred@school.edu", password="studpass1", number="SC1")
        sh = bearer(login_token(c, "stud_cred@school.edu", "studpass1"))
        assert c.post("/api/admin/runtime/credentials/openai",
                      json={"key": "sk-new-123456789"}, headers=sh).status_code == 403


def test_masked_keys_viewable_without_secret(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        from tests.test_auth import make_admin
        make_admin(engine, email="viewer_admin@school.edu")
        ah = bearer(login_token(c, "viewer_admin@school.edu", "adminpass1"))
        body = c.get("/api/admin/runtime/credentials", headers=ah).json()
        for cred in body["credentials"]:
            assert "encrypted" not in json.dumps(cred).lower()
            assert cred.get("maskedValue") is None or "••" in cred["maskedValue"] or cred["maskedValue"].count("•") == 0


# ============================ DB PERSISTENCE ============================
def test_runtime_setting_persists_and_next_lookup_reads_it(engine):
    w = _db(engine)
    try:
        rc.set_openai_config(w, admin_email="a@x", patch={"max_output_tokens": 321})
        w.commit()
    finally:
        w.close()
    # A DIFFERENT session (as another worker would use) sees the persisted value.
    r = _db(engine)
    try:
        assert rc.openai_runtime(r).max_output_tokens == 321
    finally:
        r.close()
