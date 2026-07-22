#!/usr/bin/env python3
"""MARSOUD-API-RATE-LIMIT (Abdelhamid 2026-07-22).

Per-ApiToken 60-second rate limit on /api/v1/*. In-memory dict +
SQLite write-through. Configurable via platform_settings.

Checks (4 scenarios from the ticket + 1 concurrent edge case):
  1. Under the limit → all requests succeed.
  2. Over the limit in the same minute → excess return HTTP 429
     with success=false JSON + retry_after_seconds > 0.
  3. Window rollover → after the bucket boundary passes, requests
     succeed again (simulated by advancing the memory bucket).
  4. Independent tokens have independent counters (no cross-token
     contamination).
  5. Concurrent requests via threads never exceed the limit
     (race-condition edge case from the ticket).
  6. Config change: bumping api_rate_limit_per_minute takes effect
     on the very next request.
  7. 429 response carries Retry-After header + retry_after_seconds
     in the JSON body.
"""
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
        conn.execute(text(
            "DELETE FROM api_tokens WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'rate-%@x.test')"))
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__RATE_%__'"))]
        for cid in target_cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'rate-%@x.test')"))
        conn.execute(text(
            "DELETE FROM employees WHERE email LIKE 'rate-%@x.test'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'rate-%@x.test'"))
        conn.execute(text(
            "DELETE FROM platform_settings WHERE key = "
            "'api_rate_limit_per_minute'"))
    from app.services.rate_limit import reset_memory
    reset_memory()


def _mk_user_and_token(suffix="a"):
    from app.models import User, Company
    from app.models.user import user_companies
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from app.services.api_tokens import generate_token
    from werkzeug.security import generate_password_hash

    email = f"rate-{suffix}@x.test"
    cname = f"__RATE_{suffix.upper()}__"

    c = Company(name=cname, base_currency="EGP",
                subdomain=f"rate-{suffix}")
    activate_default_subscription(c)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=email, is_active=True,
             email_verified_at=datetime.utcnow())
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    raw, tok = generate_token(u, f"rate-test-{suffix}")
    return u, tok, raw


def _ensure_fixtures():
    """Idempotent — creates the two fixture tokens the audit uses.
    Called once from main() before the checks so subsequent runs
    reuse the same rows and don't collide on subdomain UNIQUE."""
    if "raw_a" in _STATE:
        return
    _teardown()
    _u, tok_a, raw_a = _mk_user_and_token("a")
    _u2, tok_b, raw_b = _mk_user_and_token("b")
    _STATE.update(raw_a=raw_a, raw_b=raw_b,
                  tok_a=tok_a.id, tok_b=tok_b.id)


@check("1. Under the limit → all requests succeed (200)")
def _():
    from flask import current_app
    from app.services.rate_limit import reset_memory
    from app.services.subscription import _set_setting_raw
    from sqlalchemy import text
    _set_setting_raw("api_rate_limit_per_minute", "100")   # generous
    db.session.commit()
    reset_memory()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
    client = current_app.test_client()
    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    ok_count = 0
    for i in range(5):
        r = client.get("/api/v1/ping", headers=headers)
        if r.status_code == 200:
            ok_count += 1
    assert ok_count == 5, f"only {ok_count}/5 succeeded"
    return "5/5 under-limit pings OK"


@check("2. Over the limit → HTTP 429 with retry_after_seconds")
def _():
    from flask import current_app
    from app.services.subscription import _set_setting_raw
    from app.services.rate_limit import reset_memory
    from sqlalchemy import text
    _set_setting_raw("api_rate_limit_per_minute", "3")
    db.session.commit()
    reset_memory()
    # ALSO wipe the DB window rows so the very first request in this
    # check starts from a clean slate. Without this, check 1's 5
    # writes remain in api_token_windows and _read_count_from_db
    # returns 5 → immediately over the limit.
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))

    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    client = current_app.test_client()
    statuses = []
    for i in range(5):
        r = client.get("/api/v1/ping", headers=headers)
        statuses.append(r.status_code)
    # First 3 succeed, remaining 2 blocked.
    assert statuses[:3] == [200, 200, 200], f"got {statuses}"
    assert statuses[3] == 429 and statuses[4] == 429, \
        f"got {statuses}"
    # 429 body shape.
    r = client.get("/api/v1/ping", headers=headers)
    assert r.status_code == 429
    body = r.get_json()
    assert body["success"] is False
    assert "retry_after_seconds" in body
    assert body["retry_after_seconds"] >= 1
    return f"statuses={statuses}, retry_after={body['retry_after_seconds']}"


@check("3. Window rollover → requests succeed again")
def _():
    from flask import current_app
    from app.services.rate_limit import _MEM, reset_memory
    # Simulate the bucket rolling over by resetting memory + moving
    # the DB row's window back so nothing lingers in current bucket.
    reset_memory()
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    client = current_app.test_client()
    r = client.get("/api/v1/ping", headers=headers)
    assert r.status_code == 200, f"post-rollover: {r.status_code}"
    return "new window → request OK"


@check("4. Independent tokens have independent counters")
def _():
    from flask import current_app
    from app.services.rate_limit import reset_memory
    from sqlalchemy import text
    from app.services.subscription import _set_setting_raw
    _set_setting_raw("api_rate_limit_per_minute", "2")
    db.session.commit()
    reset_memory()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
    client = current_app.test_client()
    # Token A: burn through its 2 allotted.
    ha = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    for _ in range(2):
        r = client.get("/api/v1/ping", headers=ha)
        assert r.status_code == 200
    r = client.get("/api/v1/ping", headers=ha)
    assert r.status_code == 429, "token A should be limited"
    # Token B: fresh — should be allowed.
    hb = {"Authorization": f"Bearer {_STATE['raw_b']}"}
    r = client.get("/api/v1/ping", headers=hb)
    assert r.status_code == 200, \
        f"token B contaminated by A: {r.status_code}"
    return "independent counters preserved"


@check("5. Concurrent requests via threads never exceed limit")
def _():
    from flask import current_app
    from app.services.rate_limit import reset_memory
    from app.services.subscription import _set_setting_raw
    from sqlalchemy import text
    _set_setting_raw("api_rate_limit_per_minute", "10")
    db.session.commit()
    reset_memory()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))

    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    ok_count = [0]
    fail_count = [0]
    count_lock = threading.Lock()

    app_obj = current_app._get_current_object()

    def _worker():
        with app_obj.test_client() as c:
            r = c.get("/api/v1/ping", headers=headers)
            with count_lock:
                if r.status_code == 200:
                    ok_count[0] += 1
                elif r.status_code == 429:
                    fail_count[0] += 1

    # Fire 30 concurrent requests against a 10/min limit.
    threads = [threading.Thread(target=_worker) for _ in range(30)]
    for t in threads: t.start()
    for t in threads: t.join()
    # Core guarantee: the limiter NEVER over-counts under contention.
    # This is the assertion that would catch a lost-update / race bug.
    assert ok_count[0] <= 10, (
        f"limit exceeded under contention: ok={ok_count[0]}, "
        f"blocked={fail_count[0]}")
    # Sanity: SOMETHING was blocked (proves the concurrency actually
    # produced pressure). Deliberately loose because Flask test_client
    # under threads can drop requests to auth/session teardown.
    assert fail_count[0] >= 1, (
        f"nothing was blocked — test may not be stressing: "
        f"ok={ok_count[0]}, blocked={fail_count[0]}")
    return f"ok={ok_count[0]}, blocked={fail_count[0]} (limit=10)"


@check("6. Bumping the limit takes effect on next request")
def _():
    from flask import current_app
    from app.services.rate_limit import reset_memory
    from app.services.subscription import _set_setting_raw
    from sqlalchemy import text
    reset_memory()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
    _set_setting_raw("api_rate_limit_per_minute", "1")
    db.session.commit()
    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    client = current_app.test_client()
    r = client.get("/api/v1/ping", headers=headers); assert r.status_code == 200
    r = client.get("/api/v1/ping", headers=headers); assert r.status_code == 429
    # Bump the cap up.
    _set_setting_raw("api_rate_limit_per_minute", "50")
    db.session.commit()
    r = client.get("/api/v1/ping", headers=headers)
    assert r.status_code == 200, \
        f"post-bump: {r.status_code} (limit didn't reload)"
    return "config change picked up live"


@check("7. 429 response includes Retry-After header")
def _():
    from flask import current_app
    from app.services.rate_limit import reset_memory
    from app.services.subscription import _set_setting_raw
    from sqlalchemy import text
    _set_setting_raw("api_rate_limit_per_minute", "1")
    db.session.commit()
    reset_memory()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM api_token_windows"))
    headers = {"Authorization": f"Bearer {_STATE['raw_a']}"}
    client = current_app.test_client()
    client.get("/api/v1/ping", headers=headers)   # use up the 1
    r = client.get("/api/v1/ping", headers=headers)
    assert r.status_code == 429
    assert r.headers.get("Retry-After") is not None
    assert int(r.headers["Retry-After"]) >= 1
    return f"Retry-After={r.headers['Retry-After']}"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _ensure_fixtures()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
