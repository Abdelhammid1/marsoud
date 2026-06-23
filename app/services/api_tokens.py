"""MARSOUD-API-V1 — bearer token lifecycle (generate / verify / revoke).

The raw token is exposed exactly once in the response from
`generate_token()`. After that only the SHA-256 hash is persisted, and
all lookups go through `verify_token()` which uses constant-time
comparison to dodge timing attacks.
"""
import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Optional, Tuple

from app import db
from app.models import ApiToken


TOKEN_PREFIX = "mrs_live_"
TOKEN_RANDOM_LEN = 32   # bytes-base ~43 url-safe chars


def _hash(raw: str) -> str:
    """SHA-256 hex of the full raw token. Stored in the DB."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_token(user, name: str) -> Tuple[str, ApiToken]:
    """Create a new bearer token for `user`.

    Returns (raw_token, ApiToken_row). The caller MUST surface
    `raw_token` to the user immediately — it cannot be recovered later.
    """
    if not name or not name.strip():
        raise ValueError("اسم المفتاح مطلوب")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_RANDOM_LEN)
    row = ApiToken(
        user_id=user.id,
        name=name.strip()[:120],
        token_hash=_hash(raw),
        token_prefix=raw[:12] + "…",   # e.g. "mrs_live_a1b…"
        scopes="*",
    )
    db.session.add(row)
    db.session.commit()
    return raw, row


def verify_token(raw: Optional[str]) -> Optional[ApiToken]:
    """Resolve a raw bearer token to its ApiToken row, or None if it's
    missing/invalid/revoked. Uses constant-time hash comparison via
    `hmac.compare_digest` so the lookup can't be timed.

    On success, bumps `last_used_at` (best-effort — failure here doesn't
    deny the request).
    """
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    expected = _hash(raw)
    # Index lookup is fine — SHA-256 collisions are infeasible, and the
    # column has a unique index. We still constant-time compare the
    # retrieved hash to defeat any clever timing variance in the storage
    # layer.
    tok = ApiToken.query.filter_by(token_hash=expected).first()
    if not tok:
        return None
    if not hmac.compare_digest(tok.token_hash, expected):
        return None
    if tok.revoked_at is not None:
        return None
    if not tok.user or not tok.user.is_active:
        return None
    try:
        tok.last_used_at = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
    return tok


def revoke_token(tok: ApiToken) -> ApiToken:
    if tok.revoked_at is None:
        tok.revoked_at = datetime.utcnow()
        db.session.commit()
    return tok
