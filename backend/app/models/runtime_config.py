"""Runtime configuration tables: editable, secure, versioned.

These let an authorized admin change AI/voice settings and API credentials at
runtime. Secrets are stored ONLY as encrypted tokens (see app.core.crypto);
history and audit rows never contain raw secret values.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApiCredential(Base):
    """One encrypted provider credential (openai | elevenlabs).

    `encrypted_secret` is a Fernet token - the raw key is NEVER stored here and
    never returned by any API. `masked_value` is display-only."""

    __tablename__ = "api_credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    service: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False, default="")
    masked_value: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_test_status: Mapped[str] = mapped_column(String(20), nullable=False, default="never")
    last_test_message: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class SystemSetting(Base):
    """A single runtime setting override (non-secret). Absence => use env/default."""

    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False, default="")  # openai|elevenlabs|conversation|audio
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    value_type: Mapped[str] = mapped_column(String(20), nullable=False, default="str")  # str|int|float|bool
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    apply_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="immediate")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class PatientVoiceSetting(Base):
    """Per-speaker voice override. Camden and his mother are SEPARATE rows."""

    __tablename__ = "patient_voice_settings"
    __table_args__ = (UniqueConstraint("case_id", "speaker_id", name="uq_voice_case_speaker"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    speaker_id: Mapped[str] = mapped_column(String(30), nullable=False, default="patient")
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    voice_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    voice_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    model_id: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    stability: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    similarity_boost: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    style: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    speaker_boost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preview_text: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ConfigurationHistory(Base):
    """Append-only change log for non-secret settings (enables rollback).

    For credentials only SAFE metadata is recorded here - never raw keys."""

    __tablename__ = "configuration_history"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    configuration_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # openai|elevenlabs|conversation|voice|credential
    configuration_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    previous_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    new_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    changed_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    change_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
