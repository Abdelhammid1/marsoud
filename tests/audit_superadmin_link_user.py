#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-LINK-USER-01 (Abdelhamid 2026-07-29).

Batch 7 Ticket 1. Adds a super-admin route + form to attach an
email to a company. Handles two paths:
  · Existing User → INSERT (or UPDATE) user_companies row.
  · New email → CREATE Invitation + send accept email.
  · Owner role IS assignable (super-admin escape hatch — regular
    invite flow blocks owner).

Checks:
  1. Existing user, new company → row inserted with correct role
     + platform audit log entry.
  2. Existing user, already linked → role updated in-place
     (no duplicate row).
  3. Non-existing email → Invitation row created + email sender
     called (mocked).
  4. Invalid role rejected — no row / no invitation.
  5. Malformed email rejected — no row / no invitation.
  6. Cross-tenant: linking user X to company A doesn't affect
     company B's user_companies rows.
  7. Owner role IS assignable by super-admin (bypass of the
     regular invite guard).
  8. Deactivated user (is_active=False) rejected with error.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


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
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SLU_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM invitations WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'slu-%@x.test'"))


def _mk_company(suffix):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__SLU_{suffix}__", base_currency="EGP",
                 subdomain=f"slu-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    return c


def _mk_super_admin(email="slu-super@x.test"):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    su = User(email=email,
              password_hash=generate_password_hash(
                  "SuperPass1!", method="pbkdf2:sha256"),
              full_name="slu-super", is_active=True,
              is_superadmin=True,
              status=UserStatus.ACTIVE.value,
              email_verified_at=datetime.utcnow(),
              terms_version="TEST")
    db.session.add(su); db.session.commit()
    return su


def _mk_user(email, active=True):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=email, is_active=active,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.commit()
    return u


def _link(su, company_id, form):
    """POST /admin/companies/<cid>/link-user as super-admin."""
    from flask import current_app
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(su.id)
            sess["_fresh"] = True
        return client.post(
            f"/admin/companies/{company_id}/link-user",
            data=form, follow_redirects=False)


def _uc_rows(user_id, company_id):
    from sqlalchemy import text
    return db.session.execute(text(
        "SELECT role FROM user_companies "
        "WHERE user_id = :u AND company_id = :c"),
        {"u": user_id, "c": company_id}).fetchall()


@check("1. Existing user, new company → row inserted with role")
def _():
    from app.models import PlatformAuditLog
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("A")
    u = _mk_user("slu-target-1@x.test")
    db.session.commit()
    r = _link(su, c.id, {"email": u.email, "role": "accountant"})
    assert r.status_code in (302, 303), f"got {r.status_code}"
    rows = _uc_rows(u.id, c.id)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0][0] == "accountant"
    # Audit log entry.
    audit = PlatformAuditLog.query.filter(
        PlatformAuditLog.target_user_id == u.id,
        PlatformAuditLog.target_company_id == c.id,
    ).first()
    assert audit is not None, "no platform audit log entry"
    return f"linked as {rows[0][0]} + audit logged"


@check("2. Existing user, already linked → role UPDATED (no duplicate)")
def _():
    from app.models.user import user_companies
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("B")
    u = _mk_user("slu-target-2@x.test")
    db.session.commit()
    # First link as viewer.
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="viewer"))
    db.session.commit()
    # Re-link as admin (super-admin overriding).
    r = _link(su, c.id, {"email": u.email, "role": "admin"})
    assert r.status_code in (302, 303)
    rows = _uc_rows(u.id, c.id)
    assert len(rows) == 1, f"duplicate row: {rows}"
    assert rows[0][0] == "admin", f"role not updated: {rows[0][0]}"
    return f"role updated viewer → admin (no dupe)"


@check("3. Non-existing email → Invitation + email sender called")
def _():
    from app.models import Invitation
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("C")
    with patch("app.services.email.send_invitation_email",
                return_value=True) as mock_send:
        r = _link(su, c.id, {"email": "slu-new@x.test",
                                "role": "sales_manager"})
    assert r.status_code in (302, 303)
    invs = Invitation.query.filter_by(
        company_id=c.id, email="slu-new@x.test").all()
    assert len(invs) == 1, f"expected 1 invitation, got {len(invs)}"
    assert invs[0].role == "sales_manager"
    assert mock_send.called, "send_invitation_email not called"
    return f"invitation minted + email fired"


@check("4. Invalid role rejected — no side effects")
def _():
    from app.models import Invitation
    from sqlalchemy import text
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("D")
    r = _link(su, c.id, {"email": "slu-bad@x.test",
                            "role": "supreme_leader"})
    # Any redirect is fine; must NOT insert anything.
    uc_count = db.session.execute(text(
        "SELECT COUNT(*) FROM user_companies WHERE company_id = :c"),
        {"c": c.id}).scalar()
    inv_count = Invitation.query.filter_by(company_id=c.id).count()
    assert uc_count == 0 and inv_count == 0, \
        f"leak: uc={uc_count} inv={inv_count}"
    return "invalid role rejected, no rows written"


@check("5. Malformed email rejected — no side effects")
def _():
    from app.models import Invitation
    from sqlalchemy import text
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("E")
    r = _link(su, c.id, {"email": "not-an-email", "role": "viewer"})
    uc_count = db.session.execute(text(
        "SELECT COUNT(*) FROM user_companies WHERE company_id = :c"),
        {"c": c.id}).scalar()
    inv_count = Invitation.query.filter_by(company_id=c.id).count()
    assert uc_count == 0 and inv_count == 0
    return "malformed email rejected"


@check("6. Cross-tenant: linking to A doesn't affect B")
def _():
    _teardown()
    su = _mk_super_admin()
    ca = _mk_company("F1")
    cb = _mk_company("F2")
    u = _mk_user("slu-target-6@x.test")
    db.session.commit()
    _link(su, ca.id, {"email": u.email, "role": "admin"})
    # B should have zero rows for this user.
    b_rows = _uc_rows(u.id, cb.id)
    a_rows = _uc_rows(u.id, ca.id)
    assert len(a_rows) == 1
    assert len(b_rows) == 0, f"leaked into B: {b_rows}"
    return "no cross-tenant leakage"


@check("7. Owner role IS assignable by super-admin (bypass)")
def _():
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("G")
    u = _mk_user("slu-target-7@x.test")
    db.session.commit()
    r = _link(su, c.id, {"email": u.email, "role": "owner"})
    rows = _uc_rows(u.id, c.id)
    assert len(rows) == 1
    assert rows[0][0] == "owner", \
        f"owner not assigned: {rows[0][0]}"
    return "super-admin can mint owners"


@check("8. Deactivated user rejected with error")
def _():
    from sqlalchemy import text
    _teardown()
    su = _mk_super_admin()
    c = _mk_company("H")
    u = _mk_user("slu-target-8@x.test", active=False)
    db.session.commit()
    _link(su, c.id, {"email": u.email, "role": "viewer"})
    uc_count = db.session.execute(text(
        "SELECT COUNT(*) FROM user_companies WHERE company_id = :c"),
        {"c": c.id}).scalar()
    assert uc_count == 0, "deactivated user was linked (bug)"
    return "deactivated user refused"


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
