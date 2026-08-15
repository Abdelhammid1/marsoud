"""MARSOUD-MOBILE-FLUTTER — reusable /api/v1/* before_request guard.

Every mobile-facing api_v1_* blueprint installs this via
`install_api_guard(bp)`. Enforces:

  - Bearer token present + resolves to an active user.
  - `?company_id=N` param → membership check, sets `g.active_company`;
    otherwise falls back to the user's first company.
  - Per-ApiToken rate limit (same limiter the api_v1 blueprint uses).

Left in one place so a token-scoping change (e.g. adding
`ApiToken.default_company_id`) can be made once and picked up by every
API surface — the original `api_v1.py:67` block still has its own copy
for historical reasons and can migrate to this helper later.
"""
from flask import jsonify, request, g, current_app
from flask_login import current_user


def _err(message, status=400):
    r = jsonify({"error": message})
    r.status_code = status
    return r


def require_api_token():
    """Runs on every request. Returns a Flask Response to short-circuit,
    or None to continue to the view.

    Trusts the Authorization header as the sole source of truth for
    bearer identity — never `current_user` alone. `flask.g` is bound to
    the app_context, not the request context; a test harness that
    pushes an app_context and then makes multiple test_client requests
    will see `g._login_user` leak between requests, which would falsely
    authorize an unauthenticated call. Verifying the bearer header
    fresh on every request kills that leak and also matches how
    api_v1.py has always done it (see the note at line 100).
    """
    auth = request.headers.get("Authorization", "")
    tok = None
    if auth.startswith("Bearer "):
        raw = auth[7:].strip()
        if raw:
            try:
                from app.services.api_tokens import verify_token
                tok = verify_token(raw)
            except Exception:
                tok = None
    if tok is None:
        return _err("missing or invalid bearer token", 401)
    if not tok.user or not tok.user.is_active:
        return _err("inactive user", 401)

    # Load current_user for the view — normally request_loader has
    # already done this, but on some code paths (see the docstring
    # above) it hasn't or has cached a stale value. Force it fresh so
    # `current_user` inside the view is the token owner, always.
    from flask_login import login_user
    if not (current_user.is_authenticated
            and getattr(current_user, "id", None) == tok.user_id):
        # `remember=False` + no session pollution — we do not want the
        # bearer request to write a session cookie the caller can
        # replay without the bearer.
        login_user(tok.user, remember=False, force=True, fresh=False)

    # ALWAYS re-resolve — never trust a stale `g.active_company`. Same
    # reason as the bearer re-check above: g is bound to the app_context
    # in Flask 2+, so a test harness that pushes an app_context once
    # will see g leak between test_client requests. Resolving from the
    # verified token's owner keeps every request self-contained.
    companies = list(tok.user.companies)
    if not companies:
        return _err("user has no company", 403)
    cid_arg = request.args.get("company_id", type=int)
    if cid_arg:
        match = next((c for c in companies if c.id == cid_arg), None)
        if not match:
            return _err(
                f"you are not a member of company {cid_arg}", 403)
        g.active_company = match
    else:
        g.active_company = companies[0]

    # Rate limit — reuse the `tok` we resolved above (see the api_v1.py
    # note about why we can't trust Flask-Login's cached token_id).
    token_id = tok.id
    if token_id is not None:
        try:
            from app.services.rate_limit import check_and_increment
            ok, retry_after = check_and_increment(token_id)
        except Exception:
            ok, retry_after = True, None
        if not ok:
            try:
                current_app.logger.warning(
                    "api rate-limit exceeded: token=%s company=%s "
                    "endpoint=%s ip=%s retry_after=%ss",
                    token_id,
                    (g.active_company.id if g.get("active_company")
                     else None),
                    request.endpoint, request.remote_addr, retry_after,
                )
            except Exception:
                pass
            r = jsonify({
                "success": False,
                "message": "Rate limit exceeded. Please try again later.",
                "retry_after_seconds": retry_after,
            })
            r.status_code = 429
            r.headers["Retry-After"] = str(retry_after)
            return r
    return None


def install_api_guard(bp):
    """Attach `require_api_token` as a before_request handler and wire
    a JSON error envelope for common HTTP errors."""

    @bp.before_request
    def _guard():                # pragma: no cover — trivial delegation
        return require_api_token()

    @bp.errorhandler(404)
    def _e404(e):
        return _err("not found", 404)

    @bp.errorhandler(405)
    def _e405(e):
        return _err("method not allowed", 405)

    @bp.errorhandler(500)
    def _e500(e):
        return _err("internal error", 500)

    return bp
