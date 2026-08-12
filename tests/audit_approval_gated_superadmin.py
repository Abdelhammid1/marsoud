#!/usr/bin/env python3
"""MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12).

Ticket: a NEW class of superadmin user, flagged
`requires_approval=True`, whose every write attempt under
/admin/* (superadmin.* blueprint) becomes a pending row
instead of executing immediately. Abdelhamid — the primary
superadmin, flag False — approves or rejects from
/admin/pending-actions.

Checks:
  1. Schema: users.requires_approval column exists +
     pending_superadmin_actions table with all columns.
  2. Fresh user defaults to requires_approval=False (no
     accidental lock-out of existing users on migration).
  3. Restricted POST on destructive endpoint (company_toggle)
     → 302, no state change, PendingSuperadminAction row
     inserted with correct endpoint/actor/form_data.
  4. Restricted GET on destructive endpoint (company_edit)
     → 200, no pending row created (Q1: GET passes).
  5. Restricted POST on unwrapped endpoint (fail-safe)
     → 403 (a route without a DESTRUCTIVE_ENDPOINTS entry
     is dead-on-arrival for restricted users).
  6. Primary superadmin POST on destructive endpoint —
     executes directly, no queueing, company status flips.
  7. GET /admin/pending-actions as restricted → 403.
  8. GET /admin/pending-actions as primary → 200 + lists
     the pending row from check 3.
  9. Approve — decide POST as primary → company status
     flips, row.status='approved', row.decided_by set,
     audit-log lines for queued + approved.
 10. Reject — decide POST as primary → row.status='rejected',
     side-effect NOT applied.
 11. File-upload staging — restricted POSTs a fake help
     media upload → row created, file saved under
     static/staging/pending_actions/, staged_files JSON
     references it.
 12. Self-scoped exempt — restricted POSTs
     /admin/view-as/stop → 302 (pass-through, no queueing).
"""
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout on Windows so the Arabic labels don't
# blow up on print().
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db

PREFIX = "__APRGAT_"
EMAIL_PRIMARY = "aprgat-primary@x.test"
EMAIL_RESTRICTED = "aprgat-restricted@x.test"
EMAIL_NONADMIN = "aprgat-nonadmin@x.test"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.close()
    # Nuke any pending rows this suite created (before deleting
    # actor users — actor_id FK is RESTRICT on delete).
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        if "pending_superadmin_actions" in insp.get_table_names():
            conn.execute(text(
                "DELETE FROM pending_superadmin_actions"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE "
            "'" + PREFIX + "%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM invitations WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"]
                        for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} "
                        f"WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text(
                "DELETE FROM companies WHERE id = :c"),
                {"c": cid})
        conn.execute(text(
            "DELETE FROM platform_audit_logs "
            "WHERE details LIKE 'endpoint=superadmin.%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'aprgat-%@x.test'"))
    # Clean the staging dir left behind by check 11.
    from flask import current_app
    sd = (Path(current_app.root_path) / "static"
          / "staging" / "pending_actions")
    if sd.exists():
        for f in sd.iterdir():
            try:
                f.unlink()
            except OSError:
                pass


def _mk_company(suffix):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"{PREFIX}{suffix}__", base_currency="EGP",
                 subdomain=f"aprgat-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c)
    db.session.flush()
    seed_default_coa(c.id)
    return c


def _mk_user(email, *, is_superadmin=False, requires_approval=False):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=email, is_active=True,
             is_superadmin=is_superadmin,
             requires_approval=requires_approval,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u)
    db.session.commit()
    return u


def _mk_users():
    """The 3-user fixture: primary, restricted, non-admin."""
    primary = _mk_user(EMAIL_PRIMARY,
                       is_superadmin=True,
                       requires_approval=False)
    restricted = _mk_user(EMAIL_RESTRICTED,
                          is_superadmin=True,
                          requires_approval=True)
    non_admin = _mk_user(EMAIL_NONADMIN,
                         is_superadmin=False,
                         requires_approval=False)
    return primary, restricted, non_admin


def _client_as(user_id):
    """Return a Flask test_client already logged in as user_id.

    Also drops Flask-Login's per-app-context cache
    (`g._login_user`) so that a previous request in this app
    context — e.g. one made through a DIFFERENT client — can't
    poison `current_user` for this one. Flask's test_client in
    Flask 2.2+ reuses the outer app_context, so `g` is shared
    across successive requests unless we clear it here."""
    from flask import current_app, g
    if "_login_user" in g:
        del g._login_user
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


# ── checks ────────────────────────────────────────────────── #

@check("1. Schema — column + table exist with all columns")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    user_cols = {c["name"] for c in insp.get_columns("users")}
    assert "requires_approval" in user_cols, \
        "users.requires_approval missing"
    tables = set(insp.get_table_names())
    assert "pending_superadmin_actions" in tables, \
        "pending_superadmin_actions table missing"
    pa_cols = {c["name"] for c in insp.get_columns(
        "pending_superadmin_actions")}
    for c in ("id", "actor_id", "endpoint", "method", "url_path",
               "view_args", "form_data", "staged_files", "status",
               "created_at", "decided_by", "decided_at",
               "decision_note"):
        assert c in pa_cols, f"column missing: {c}"
    return f"users+{len(pa_cols)} col pending table OK"


@check("2. Fresh user defaults to requires_approval=False")
def _():
    _teardown()
    u = _mk_user("aprgat-fresh@x.test")
    assert u.requires_approval is False, \
        f"got {u.requires_approval!r}, want False"
    # Cleanup this local user (not in the aprgat-primary/... set).
    from sqlalchemy import text
    db.session.execute(text(
        "DELETE FROM users WHERE email = 'aprgat-fresh@x.test'"))
    db.session.commit()
    return "default False — no accidental lock-out"


@check("3. Restricted POST → queued, no side-effect")
def _():
    from app.models import Company, PendingSuperadminAction
    _teardown()
    primary, restricted, _na = _mk_users()
    c = _mk_company("C3")
    original_status = c.status
    c_id = c.id
    r = _client_as(restricted.id).post(
        f"/admin/companies/{c_id}/toggle",
        follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    # Redirect target = /admin/pending-actions
    assert "/admin/pending-actions" in r.headers.get("Location", ""), \
        f"bad redirect: {r.headers.get('Location')}"
    c2 = db.session.get(Company, c_id)
    assert c2.status == original_status, \
        f"status leaked: {original_status} → {c2.status}"
    rows = (PendingSuperadminAction.query
            .filter_by(endpoint="superadmin.company_toggle",
                        actor_id=restricted.id).all())
    assert len(rows) == 1, f"expected 1 pending row, got {len(rows)}"
    va = json.loads(rows[0].view_args or "{}")
    assert va.get("company_id") == c_id, \
        f"view_args off: {va}"
    return f"queued row #{rows[0].id}, status unchanged"


@check("4. Restricted GET on destructive endpoint → 200, no queue")
def _():
    from app.models import PendingSuperadminAction
    _teardown()
    primary, restricted, _na = _mk_users()
    c = _mk_company("C4")
    r = _client_as(restricted.id).get(
        f"/admin/companies/{c.id}/edit", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    n = PendingSuperadminAction.query.count()
    assert n == 0, f"GET created a pending row (n={n})"
    return "GET passes, no queue"


@check("5. Restricted POST on unwrapped endpoint → 403 (fail-safe)")
def _():
    """The `consent_index` route accepts GET only, so a POST
    there returns 405 — not what we want. Use the audit
    endpoint which similarly is not registered.

    Better: use `subscription_settings` — WAIT, it IS
    registered. So we need an ACTUAL non-registered but
    POST-accepting endpoint. The safest is to fake one by
    hitting `/admin/subscriptions` (index — GET only,
    would 405).

    The cleanest fail-safe test: temporarily register a
    throw-away rule, POST it, expect 403. That requires
    Flask internals we don't want to touch here. Simpler
    approach — verify the gate directly: call gate_request()
    inside a synthesized POST context to an unknown endpoint
    and assert it aborts."""
    from flask import current_app
    from werkzeug.exceptions import Forbidden
    from werkzeug.routing import Rule
    from app.services.superadmin_approval import gate_request
    from flask_login import login_user
    _teardown()
    primary, restricted, _na = _mk_users()
    with current_app.test_request_context(
            "/admin/fake-unregistered-endpoint",
            method="POST"):
        # request.endpoint is a computed property on
        # (url_rule.endpoint) — set url_rule to a fake Rule
        # to simulate a superadmin.* route that was added
        # without being registered in DESTRUCTIVE_ENDPOINTS.
        from flask import request as flask_req
        flask_req.url_rule = Rule(
            "/admin/fake-unregistered-endpoint",
            endpoint="superadmin.does_not_exist_route")
        login_user(restricted)
        raised = False
        try:
            gate_request()
        except Forbidden:
            raised = True
    assert raised, "gate did NOT abort — fail-safe broken"
    return "unregistered POST → 403"


@check("6. Primary POST executes directly (no queue)")
def _():
    from app.models import Company, PendingSuperadminAction
    _teardown()
    primary, _r, _na = _mk_users()
    c = _mk_company("C6")
    c_id = c.id
    orig = c.status
    r = _client_as(primary.id).post(
        f"/admin/companies/{c_id}/toggle", follow_redirects=False)
    assert r.status_code in (302, 303)
    c2 = db.session.get(Company, c_id)
    assert c2.status != orig, \
        f"primary POST didn't execute: {orig} → {c2.status}"
    n = PendingSuperadminAction.query.count()
    assert n == 0, f"primary POST created pending row (n={n})"
    return f"primary flipped {orig} → {c2.status} directly"


@check("7. GET /admin/pending-actions as restricted → 403")
def _():
    _teardown()
    _p, restricted, _na = _mk_users()
    r = _client_as(restricted.id).get("/admin/pending-actions")
    assert r.status_code == 403, f"got {r.status_code}"
    return "restricted user cannot view approval inbox"


@check("8. GET /admin/pending-actions as primary → 200, lists rows")
def _():
    _teardown()
    primary, restricted, _na = _mk_users()
    c = _mk_company("C8")
    # Queue a row by having restricted POST toggle.
    _client_as(restricted.id).post(
        f"/admin/companies/{c.id}/toggle", follow_redirects=False)
    r = _client_as(primary.id).get("/admin/pending-actions")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "company_toggle" in body or "تغيير حالة" in body, \
        "row not listed"
    return "primary sees the queue"


@check("9. Approve — executes + audit logs written")
def _():
    from app.models import (
        Company, PendingSuperadminAction, PlatformAuditLog,
    )
    _teardown()
    primary, restricted, _na = _mk_users()
    c = _mk_company("C9")
    c_id = c.id
    orig = c.status
    _client_as(restricted.id).post(
        f"/admin/companies/{c_id}/toggle", follow_redirects=False)
    row = PendingSuperadminAction.query.filter_by(
        actor_id=restricted.id).first()
    assert row is not None, "queue row missing"
    r = _client_as(primary.id).post(
        "/admin/pending-actions/decide",
        data={"action_id": str(row.id), "decision": "approve"},
        follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    db.session.expire_all()
    row2 = db.session.get(PendingSuperadminAction, row.id)
    assert row2.status == "approved", \
        f"row.status = {row2.status!r}"
    assert row2.decided_by == primary.id, \
        f"decided_by = {row2.decided_by}"
    c2 = db.session.get(Company, c_id)
    assert c2.status != orig, \
        f"replay didn't flip status: {orig} → {c2.status}"
    # Audit-log lines: queued + approved.
    queued = PlatformAuditLog.query.filter_by(
        action="superadmin_action_queued").count()
    approved = PlatformAuditLog.query.filter_by(
        action="superadmin_action_approved").count()
    assert queued >= 1 and approved >= 1, \
        f"audit lines missing: queued={queued} approved={approved}"
    return (f"row approved, status {orig}→{c2.status}, "
            f"audit lines OK")


@check("10. Reject — row rejected, side-effect NOT applied")
def _():
    from app.models import (
        Company, PendingSuperadminAction, PlatformAuditLog,
    )
    _teardown()
    primary, restricted, _na = _mk_users()
    c = _mk_company("C10")
    c_id = c.id
    orig = c.status
    _client_as(restricted.id).post(
        f"/admin/companies/{c_id}/toggle", follow_redirects=False)
    row = PendingSuperadminAction.query.filter_by(
        actor_id=restricted.id).first()
    r = _client_as(primary.id).post(
        "/admin/pending-actions/decide",
        data={"action_id": str(row.id), "decision": "reject",
              "note": "غير مناسب الآن"},
        follow_redirects=False)
    assert r.status_code in (302, 303)
    db.session.expire_all()
    row2 = db.session.get(PendingSuperadminAction, row.id)
    assert row2.status == "rejected", \
        f"row.status = {row2.status!r}"
    assert row2.decision_note == "غير مناسب الآن", \
        f"note = {row2.decision_note!r}"
    c2 = db.session.get(Company, c_id)
    assert c2.status == orig, \
        f"status leaked on reject: {orig} → {c2.status}"
    rejected = PlatformAuditLog.query.filter_by(
        action="superadmin_action_rejected").count()
    assert rejected >= 1, "no rejected audit log line"
    return f"reject OK, {orig} preserved"


@check("11. File-upload staging — row + file on disk")
def _():
    from app.models import (
        HelpArticle, PendingSuperadminAction,
    )
    from flask import current_app
    _teardown()
    primary, restricted, _na = _mk_users()
    # Need a HelpArticle to attach to.
    art = HelpArticle(
        module_key="__aprgat_test__",
        title_ar="TEST",
        is_published=True,
    )
    db.session.add(art)
    db.session.commit()
    art_id = art.id
    file_bytes = b"fakeimagebytes"
    r = _client_as(restricted.id).post(
        f"/admin/help/{art_id}/media",
        data={"kind": "IMAGE",
              "file": (io.BytesIO(file_bytes),
                        "sample.png")},
        content_type="multipart/form-data",
        follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    row = PendingSuperadminAction.query.filter_by(
        endpoint="superadmin.help_add_media",
        actor_id=restricted.id).first()
    assert row is not None, "no pending row for help_add_media"
    assert row.staged_files, "staged_files empty"
    staged = json.loads(row.staged_files)
    assert "file" in staged, \
        f"field missing from staged_files: {staged}"
    disk = staged["file"]
    assert Path(disk).exists(), f"staged file not on disk: {disk}"
    assert Path(disk).read_bytes() == file_bytes, \
        "staged file content mismatch"
    # Cleanup the article + staging file.
    db.session.delete(art)
    db.session.commit()
    try:
        Path(disk).unlink()
    except OSError:
        pass
    return "file staged + JSON references it"


@check("12. Self-scoped exempt — view_as_stop passes through")
def _():
    """view_as_stop has @login_required only (no
    @superadmin_required), so the gate wouldn't even run.
    Verify the endpoint is reachable for the restricted user
    without queueing, and that it's in our SELF_SCOPED_EXEMPT
    set (documentation invariant)."""
    from app.services.superadmin_approval import SELF_SCOPED_EXEMPT
    assert "superadmin.view_as_stop" in SELF_SCOPED_EXEMPT, \
        "view_as_stop should be in SELF_SCOPED_EXEMPT"
    _teardown()
    _p, restricted, _na = _mk_users()
    r = _client_as(restricted.id).post(
        "/admin/view-as/stop", follow_redirects=False)
    # 302 (redirect back to dashboard) or 200 — anything but 403.
    assert r.status_code != 403, \
        f"restricted got 403 on view_as_stop: {r.status_code}"
    return f"view_as_stop reachable ({r.status_code})"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
