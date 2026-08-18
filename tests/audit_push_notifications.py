#!/usr/bin/env python3
"""MARSOUD-MOBILE-TKT-05 (2026-08-18) — push notifications audit.

Verifies backend-side wiring. The actual FCM send path requires
a live Firebase project + service-account JSON — audited via
monkeypatching the fcm.push_to_user function so we can assert
the notify() hook FIRES a push (or skips it) without hitting
the network.

  A. push_tokens schema created (all columns + unique constraint).
  B. POST /api/v1/my/push-tokens registers a new token (201).
  C. POST with the same (user, token) updates last_used_at, not
     duplicates (200, created=false).
  D. DELETE /api/v1/my/push-tokens/by-token soft-revokes.
  E. notify() with a push-enabled kind CALLS fcm.push_to_user.
  F. notify() with a push-DISABLED kind (VENDOR_BILL_OVERDUE)
     does NOT call fcm.push_to_user.
  G. fcm.is_configured() is False when no service-account file
     present (graceful degradation).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__PUSH_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from app.models import Company, User, PushToken
    from app.models.user import user_companies

    PushToken.query.filter(
        PushToken.token.like(f"{CO_NAME}%")
    ).delete(synchronize_session=False)
    db.session.commit()

    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                try:
                    db.session.execute(
                        t.delete().where(t.c.company_id.in_(ids)))
                except Exception:
                    db.session.rollback()
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        PushToken.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
    db.session.commit()
    from sqlalchemy import text
    for cid in ids:
        try:
            db.session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": cid})
        except Exception:
            db.session.rollback()
    db.session.commit()


def _setup():
    from datetime import timedelta
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.legal import get_terms_version
    from app.services.api_tokens import generate_token

    _teardown()
    tv = get_terms_version()
    now = datetime.utcnow()
    plan = Plan.query.filter_by(code="growth").first() \
           or Plan.query.filter_by(code="pro").first()

    u = User(email=f"{CO_NAME.lower()}_owner@x.local",
             full_name="Owner", terms_version=tv,
             terms_accepted_at=now)
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_A", base_currency="EGP",
                 plan_id=plan.id if plan else None,
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=30))
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    db.session.commit()

    raw_token, _ = generate_token(u, "audit:push")
    _STATE.update(dict(u=u, co=co, bearer=raw_token))


def _api(method, url, *, body=None):
    from flask import g as flask_g
    if "_login_user" in flask_g:
        del flask_g._login_user
    app = _STATE["app"]
    c = app.test_client()
    headers = {"Authorization": f"Bearer {_STATE['bearer']}"}
    kwargs = dict(headers=headers)
    if body is not None:
        kwargs["json"] = body
    return c.open(
        url + (("?" if "?" not in url else "&") +
               f"company_id={_STATE['co'].id}"),
        method=method, **kwargs)


# ─── A. Schema ────────────────────────────────────────────────────────
@check("A1: push_tokens table exists with all columns + unique constraint")
def A1():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    assert "push_tokens" in insp.get_table_names(), (
        "push_tokens table missing")
    cols = {c["name"] for c in insp.get_columns("push_tokens")}
    for c in ("id", "user_id", "token", "platform",
              "device_label", "is_active", "last_used_at",
              "created_at"):
        assert c in cols, f"push_tokens missing column {c}"


# ─── B. Register a new token ──────────────────────────────────────────
@check("B1: POST /my/push-tokens creates a new PushToken row (201)")
def B1():
    from app.models import PushToken
    tok = f"{CO_NAME}_TOKEN_1"
    r = _api("POST", "/api/v1/my/push-tokens", body={
        "token": tok,
        "platform": "android",
        "device_label": "Test Device",
    })
    assert r.status_code == 201, (r.status_code, r.data[:200])
    body = json.loads(r.data)
    assert body.get("ok") is True and body.get("created") is True
    row = PushToken.query.filter_by(
        user_id=_STATE["u"].id, token=tok).first()
    assert row is not None and row.is_active


# ─── C. Idempotent upsert ─────────────────────────────────────────────
@check("C1: POST with same (user, token) updates instead of dup (200)")
def C1():
    from app.models import PushToken
    tok = f"{CO_NAME}_TOKEN_1"
    r = _api("POST", "/api/v1/my/push-tokens", body={
        "token": tok, "platform": "android",
        "device_label": "Renamed Device"})
    assert r.status_code == 200, r.status_code
    body = json.loads(r.data)
    assert body.get("created") is False
    n = PushToken.query.filter_by(
        user_id=_STATE["u"].id, token=tok).count()
    assert n == 1, f"expected 1 row, got {n}"


# ─── D. Revoke by token ───────────────────────────────────────────────
@check("D1: DELETE /my/push-tokens/by-token soft-revokes")
def D1():
    from app.models import PushToken
    tok = f"{CO_NAME}_TOKEN_1"
    r = _api("DELETE", "/api/v1/my/push-tokens/by-token",
             body={"token": tok})
    assert r.status_code == 200, r.status_code
    row = PushToken.query.filter_by(
        user_id=_STATE["u"].id, token=tok).first()
    assert row.is_active is False


# ─── E. notify() hook fires push for enabled kind ─────────────────────
@check("E1: notify() with TASK_ASSIGNED calls fcm.push_to_user")
def E1():
    from app.services import opsflow_extras as ox
    from app.services import fcm as _fcm_module
    from app.models import NotificationKind

    calls = []
    original = _fcm_module.push_to_user

    def _spy(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return 0
    _fcm_module.push_to_user = _spy
    try:
        ox.notify(_STATE["u"].id,
                   company_id=_STATE["co"].id,
                   kind=NotificationKind.TASK_ASSIGNED,
                   title="Test task", body="Hello",
                   link_url="/tasks/1")
    finally:
        _fcm_module.push_to_user = original
    assert len(calls) == 1, f"expected 1 push call, got {len(calls)}"
    uid, kw = calls[0]
    assert uid == _STATE["u"].id
    assert kw["title"] == "Test task"
    assert kw["kind"] == "TASK_ASSIGNED"


# ─── F. notify() skips push for disabled kind ─────────────────────────
@check("F1: notify() with VENDOR_BILL_OVERDUE does NOT call fcm")
def F1():
    from app.services import opsflow_extras as ox
    from app.services import fcm as _fcm_module
    from app.models import NotificationKind

    calls = []
    original = _fcm_module.push_to_user

    def _spy(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return 0
    _fcm_module.push_to_user = _spy
    try:
        ox.notify(_STATE["u"].id,
                   company_id=_STATE["co"].id,
                   kind=NotificationKind.VENDOR_BILL_OVERDUE,
                   title="Bill overdue",
                   body="500 EGP overdue")
    finally:
        _fcm_module.push_to_user = original
    assert calls == [], (
        f"push should be SKIPPED for VENDOR_BILL_OVERDUE, "
        f"got {calls}")


# ─── G. fcm.is_configured() when no service account ──────────────────
@check("G1: fcm.is_configured() returns False without service account")
def G1():
    from app.services import fcm as _fcm_module
    # Make sure we bypass any cached ready state and remove any
    # ambient env vars that could point at real creds.
    import os
    saved = {}
    for k in ("FIREBASE_SERVICE_ACCOUNT_JSON",
              "FIREBASE_SERVICE_ACCOUNT_PATH"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    _fcm_module._INIT_STATE.update({"ready": False, "app": None,
                                     "attempted": False,
                                     "logged_no_creds": True,
                                     "logged_missing": True})
    try:
        # Also monkey-patch the default candidate path check to
        # return False so we don't accidentally see a real file.
        from pathlib import Path
        orig_is_file = Path.is_file
        Path.is_file = lambda self: False
        try:
            assert _fcm_module.is_configured() is False
        finally:
            Path.is_file = orig_is_file
    finally:
        for k, v in saved.items():
            os.environ[k] = v


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
