"""MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) — symmetric at-rest
encryption for tenant secrets.

Today's only consumer is `CompanySMTP.password_encrypted`, but the
helper is deliberately generic so future tenant-credential storage
(e.g. provider API keys) can reuse the same pair.

Backing: `cryptography.Fernet`.  The Fernet key is derived from the
app's `SECRET_KEY` via SHA256 + urlsafe-base64, which means:

  * The same deployment can decrypt its own data across restarts
    (deterministic derivation).
  * Rotating `SECRET_KEY` makes every stored secret unreadable.
    This is a documented operational caveat — if you rotate, you
    must re-enter every tenant's SMTP password.

If you need per-secret rotation independent of SECRET_KEY, swap
`_key()` for a dedicated `SMTP_ENCRYPTION_KEY` env var.  Kept
minimal here because the ticket's threat model is "database leak
without the Flask secret", not "SECRET_KEY also leaked".
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _key() -> bytes:
    sk = current_app.config["SECRET_KEY"]
    if isinstance(sk, str):
        sk = sk.encode()
    # Fernet wants a 32-byte urlsafe-base64 key.  SHA256 gives us
    # exactly 32 bytes; base64-encode so Fernet accepts it directly.
    return base64.urlsafe_b64encode(hashlib.sha256(sk).digest())


def encrypt(plain: str | None) -> bytes:
    """Encrypt a plaintext string.  Empty / None → empty bytes
    (represents 'no secret stored')."""
    if not plain:
        return b""
    return Fernet(_key()).encrypt(plain.encode("utf-8"))


def decrypt(token: bytes | None) -> str | None:
    """Decrypt a token produced by `encrypt`.  Returns None on empty
    input OR on tamper / rotated-key — callers treat None as
    'password unavailable' and surface the right error."""
    if not token:
        return None
    try:
        return Fernet(_key()).decrypt(token).decode("utf-8")
    except InvalidToken:
        return None
