"""MARSOUD-MOBILE-FLUTTER — JSON auth endpoints for the mobile app.

Mounted at /api/v1/auth. Endpoints:

    POST /api/v1/auth/login           — email + password → bearer token
    POST /api/v1/auth/logout          — revoke current bearer token
    POST /api/v1/auth/change-password — old + new → 200 (also usable by web)

Design:
- Login is the only endpoint in the whole `/api/*` tree that must work
  WITHOUT a bearer — it's what mints the bearer in the first place. So
  it lives on its own blueprint (this file), NOT under `api_v1_bp` whose
  before_request forces bearer auth on every route.
- Failed logins bump `User.failed_login_attempts` and lock the account
  for 15 minutes at 5 failures — same policy as `auth.login` (web).
  A locked account rejects even the correct password until the window
  passes, so the "wrong pw is faster than correct pw" timing signal
  can't be used mid-brute-force.
- Tokens are named `mobile:<device>` so they're distinguishable from
  Ibrahim's manually-minted CLI tokens on /settings/api-tokens/.
- All responses follow the `/api/*` JSON error contract enforced by the
  global `_api_http_error` handler in `app/__init__.py`.
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app import db
from app.models import User
from app.services.api_tokens import (
    generate_token, verify_token, revoke_token,
)
from app.services.password_policy import validate_password


bp = Blueprint("api_v1_auth", __name__)


# ─── Helpers ──────────────────────────────────────────────────────────
def _err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def _user_public(u):
    """Same shape mobile expects everywhere (matches api_v1._user_brief)."""
    return {"id": u.id, "name": u.full_name, "email": u.email}


def _company_public(c, role=None):
    return {"id": c.id, "name": c.name, "role": role}


# ─── Login ────────────────────────────────────────────────────────────
@bp.route("/login", methods=["POST"])
def login():
    """Body: `{email, password, device_name?}`

    Success: `200 {token, user, companies, default_company_id}`
    - `token` — raw bearer, shown once. Store in flutter_secure_storage.
    - `companies[i].role` — the caller's role in that company (owner /
      admin / hr_manager / employee / …). Mobile uses this to pick nav.
    - `default_company_id` — first company; mobile can override per
      request via `?company_id=N` (same as any api_v1 endpoint).

    Failure:
    - 400 `missing_credentials` — email/password blank
    - 401 `invalid_credentials` — wrong password OR unknown email
      (same message to avoid enumerating valid emails)
    - 403 `account_locked` — 5 failures in 15 min
    - 403 `account_inactive` — pending activation / disabled
    - 403 `no_companies` — user exists but is in zero companies
    """
    body = request.get_json(silent=True) or request.form
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    device = (body.get("device_name") or "").strip()[:40]

    if not email or not password:
        return _err("missing_credentials", 400)

    user = User.query.filter_by(email=email).first()

    # Lock window check FIRST — reject even a correct password while
    # locked so brute-force can't use timing to distinguish.
    if user and user.locked_until and user.locked_until > datetime.utcnow():
        remaining = int(
            (user.locked_until - datetime.utcnow()).total_seconds() // 60) + 1
        return _err("account_locked", 403,
                    retry_after_minutes=remaining)

    if not user or not user.check_password(password):
        # Only count failed attempts when the user exists — avoid the
        # lockout counter itself becoming an email-enumeration oracle.
        if user:
            user.failed_login_attempts = (
                (user.failed_login_attempts or 0) + 1)
            if user.failed_login_attempts >= 5:
                user.locked_until = datetime.utcnow() + timedelta(minutes=15)
            db.session.commit()
        return _err("invalid_credentials", 401)

    if not user.is_active:
        return _err("account_inactive", 403)

    # Which companies is this user in? Filter out SUSPENDED, mirroring
    # web /login behaviour.
    active_companies = [c for c in user.companies
                        if (c.status or "ACTIVE") != "SUSPENDED"]
    if user.companies and not active_companies and not user.is_superadmin:
        return _err("all_companies_suspended", 403)
    if not active_companies and not user.is_superadmin:
        return _err("no_companies", 403)

    # Resolve role per company through the user_companies association
    # table. Same source of truth the web sidebar reads.
    from app.models.user import user_companies
    rows = db.session.execute(
        user_companies.select().where(
            user_companies.c.user_id == user.id
        )
    ).fetchall()
    role_by_cid = {r.company_id: r.role for r in rows}

    # Reset failure counter + stamp last-login now that we know it's a
    # real successful login.
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()
    db.session.commit()

    # Mint the token — name it after the device so revoking from the
    # web /settings/api-tokens page is legible.
    token_name = f"mobile:{device or 'unknown'}"
    try:
        raw_token, tok = generate_token(user, token_name)
    except ValueError as e:
        return _err(str(e), 400)

    return jsonify({
        "token": raw_token,
        "token_id": tok.id,
        "user": _user_public(user),
        "companies": [
            _company_public(c, role=role_by_cid.get(c.id))
            for c in active_companies
        ],
        "default_company_id": active_companies[0].id if active_companies else None,
    })


# ─── Logout ───────────────────────────────────────────────────────────
@bp.route("/logout", methods=["POST"])
def logout():
    """Revokes the bearer token the request came in with. The raw token
    itself is never persisted, so we resolve it from the header, look up
    the ApiToken row by hash, and set `revoked_at`.

    Idempotent: hitting /logout twice with the same token returns
    `{ok: true}` both times."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return _err("missing bearer token", 401)
    tok = verify_token(auth[7:].strip())
    if not tok:
        # Already revoked / invalid — still return OK. A mobile client
        # calling logout on an already-dead token expects success.
        return jsonify({"ok": True})
    revoke_token(tok)
    return jsonify({"ok": True})


# ─── Change password ──────────────────────────────────────────────────
@bp.route("/change-password", methods=["POST"])
def change_password():
    """Body: `{old, new}` — must be authenticated by bearer or session.

    Same password-policy validator the web self-service uses. Reused
    from portal_emp.change_password (hr_self_service.py:398).
    """
    # This endpoint requires an authenticated user. Since it lives on
    # its own blueprint (not api_v1_bp), the before_request bearer
    # gate doesn't run here — enforce auth manually.
    if not current_user.is_authenticated:
        return _err("missing or invalid bearer token", 401)

    body = request.get_json(silent=True) or request.form
    old = body.get("old") or body.get("old_password") or ""
    new = body.get("new") or body.get("new_password") or ""

    if not current_user.check_password(old):
        return _err("wrong_old_password", 400)
    ok, reason = validate_password(new)
    if not ok:
        return _err(reason, 400)
    if new == old:
        return _err("new_must_differ", 400)

    current_user.set_password(new)
    db.session.commit()
    return jsonify({"ok": True})
