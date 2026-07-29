"""Authenticated encryption for secrets stored in the database.

API keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a
server-side key loaded from CONFIG_ENCRYPTION_KEY. The key lives OUTSIDE the
database (env / server secret) and is never exposed through any API.

If no key is configured, secret storage is DISABLED (writes raise) rather than
silently falling back to plaintext - we never store secrets unencrypted.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class EncryptionUnavailableError(RuntimeError):
    """Raised when a secret write/read is attempted without an encryption key."""


def _derive_fernet_key(raw: str) -> bytes:
    """Accept either a real Fernet key (44-char urlsafe b64) or any passphrase.

    A passphrase is stretched to a 32-byte key via SHA-256 so operators can set
    CONFIG_ENCRYPTION_KEY to any sufficiently strong secret."""
    raw = raw.strip()
    try:
        if len(raw) == 44 and base64.urlsafe_b64decode(raw):
            return raw.encode("ascii")
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encryption_available() -> bool:
    return bool(get_settings().config_encryption_key.strip())


def _fernet() -> Fernet:
    key = get_settings().config_encryption_key.strip()
    if not key:
        raise EncryptionUnavailableError(
            "CONFIG_ENCRYPTION_KEY is not set; secure secret storage is disabled."
        )
    return Fernet(_derive_fernet_key(key))


def encrypt_secret(plaintext: str) -> str:
    """Return an encrypted token (safe to store). Raises if no key configured."""
    if not plaintext:
        raise ValueError("Refusing to encrypt an empty secret.")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a stored token. Raises EncryptionUnavailableError if unreadable."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise EncryptionUnavailableError("Stored secret could not be decrypted.") from exc


def mask_secret(value: str, keep_end: int = 4) -> str:
    """Reveal only a short head and tail; never the middle. Empty -> ''."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= keep_end + 3:
        return "•" * len(value)
    head = value[: min(4, len(value) - keep_end)]
    return f"{head}••••{value[-keep_end:]}"
