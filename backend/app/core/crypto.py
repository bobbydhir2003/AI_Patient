"""Authenticated encryption for secrets stored in the database.

API keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a
server-side key derived from CONFIG_ENCRYPTION_KEY. The key material lives
OUTSIDE the database (env / server secret) and is never exposed through any API.

If no key is configured, secret storage is DISABLED (writes raise) rather than
silently falling back to plaintext - we never store secrets unencrypted.

Key derivation (A12):
- NEW tokens (v2) derive the Fernet key with PBKDF2-HMAC-SHA256 using a random
  per-secret salt and a high iteration count. The token carries explicit version
  metadata and the salt: ``v2$<salt_b64>$<fernet_token>``.
- LEGACY tokens (v1) were a bare Fernet token whose key came from a single,
  UNSALTED SHA-256 of the passphrase. These remain decryptable via the legacy
  path so previously stored credentials are never lost. Call sites re-encrypt
  them to v2 opportunistically (see runtime_config_service).
Version is detected from explicit metadata (the ``v2$`` prefix), never guessed.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import get_settings

_V2_PREFIX = "v2"
_V2_SEP = "$"
_PBKDF2_ITERATIONS = 480_000  # OWASP-recommended floor for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


class EncryptionUnavailableError(RuntimeError):
    """Raised when a secret write/read is attempted without an encryption key."""


def _passphrase() -> str:
    key = get_settings().config_encryption_key.strip()
    if not key:
        raise EncryptionUnavailableError(
            "CONFIG_ENCRYPTION_KEY is not set; secure secret storage is disabled."
        )
    return key


# --------------------------------------------------------------------- v1 (legacy)
def _derive_legacy_fernet_key(raw: str) -> bytes:
    """LEGACY unsalted key derivation. Retained ONLY to decrypt v1 tokens.

    Accepts either a real Fernet key (44-char urlsafe b64) or any passphrase
    stretched to 32 bytes via a single SHA-256 (the old, weak scheme)."""
    raw = raw.strip()
    try:
        if len(raw) == 44 and base64.urlsafe_b64decode(raw):
            return raw.encode("ascii")
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


# --------------------------------------------------------------------- v2 (current)
def _derive_v2_fernet_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encryption_available() -> bool:
    return bool(get_settings().config_encryption_key.strip())


def is_legacy_token(token: str) -> bool:
    """True if the stored token uses the legacy (v1) unsalted format."""
    return not (token or "").startswith(_V2_PREFIX + _V2_SEP)


def encrypt_secret(plaintext: str) -> str:
    """Return a versioned, salted encrypted token (safe to store). Raises if no
    key is configured."""
    if not plaintext:
        raise ValueError("Refusing to encrypt an empty secret.")
    passphrase = _passphrase()
    salt = os.urandom(_SALT_BYTES)
    fernet = Fernet(_derive_v2_fernet_key(passphrase, salt))
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    return f"{_V2_PREFIX}{_V2_SEP}{salt_b64}{_V2_SEP}{token}"


def decrypt_secret(token: str) -> str:
    """Decrypt a stored token (v2 or legacy v1). Raises EncryptionUnavailableError
    if unreadable. Format is detected from explicit version metadata - never by
    trial-and-error guessing."""
    passphrase = _passphrase()
    try:
        if not is_legacy_token(token):
            _prefix, salt_b64, fernet_token = token.split(_V2_SEP, 2)
            salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
            fernet = Fernet(_derive_v2_fernet_key(passphrase, salt))
            return fernet.decrypt(fernet_token.encode("ascii")).decode("utf-8")
        # Legacy path: bare Fernet token, unsalted SHA-256 key.
        fernet = Fernet(_derive_legacy_fernet_key(passphrase))
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
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
