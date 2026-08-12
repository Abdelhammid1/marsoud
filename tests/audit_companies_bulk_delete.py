#!/usr/bin/env python3
"""MARSOUD-COMPANIES-BULK-DELETE (2026-08-12).

Ticket: bulk soft-delete on /admin/companies + a dedicated
/admin/companies/deleted page with bulk hard-delete gated
by a typed "تأكيد" and auto-JSON backup before the wipe.

Checks:
  1. Bulk soft-delete happy path — 3 valid ids.
  2. Bulk soft-delete with a bad id — 2 valid + 1 missing.
  3. Idempotency — second POST is a no-op.
  4. Non-superadmin → 403 on both bulk routes.
  5. GET /admin/companies/deleted empty state → 200.
  6. Lists soft-deleted rows.
  7. Main /admin/companies HIDES soft-deleted rows.
  8. Bulk hard-delete WITHOUT "تأكيد" → refused, no wipe,
     no backup file.
  9. Bulk hard-delete WITH "تأكيد" → rows nuked + audit
     line written + backup file exists.
 10. Backup captures child-table rows (customers).
"""
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from werkzeug.datastructures import MultiDict

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db

PREFIX = "__CBLK_"
EMAIL_SUPER = "cblk-super@x.test"
EMAIL_USER = "cblk-user@x.test"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _md(pairs):
    """Small helper — build a MultiDict from a list of tuples."""
    return MultiDict(pairs)


def _backup_dir():
    from flask import current_app
    return (Path(current_app.root_path) / "static"
            / "backups" / "company_purges")


def _clean_backup_files_for(cid):
    d = _backup_dir()
    if not d.exists():
        return
    for f in d.iterdir():
        if f.name.startswith(f"{cid}_") and f.suffix == ".json":
            try:
                f.unlink()
            except OSError:
                pass


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '"
            + PREFIX + "%__'"))]
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
            "WHERE action LIKE 'company_%_delete%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'cblk-%@x.test'"))
    # Blast leftover backup files this suite created.
    d = _backup_dir()
    if d.exists():
        for f in d.iterdir():
            if f.suffix != ".json":
                continue
            try:
                content = f.read_text(encoding="utf-8")
                if PREFIX in content:
                    f.unlink()
            except (OSError, UnicodeDecodeError):
                pass


def _mk_company(suffix):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"{PREFIX}{suffix}__", base_currency="EGP",
                 subdomain=f"cblk-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c)
    db.session.flush()
    seed_default_coa(c.id)
    return c


def _users_has_requires_approval():
    """Detect the parallel-branch column so tests still pass
    even when the approval-gated migration was applied
    locally against this branch's User model (which doesn't
    declare requires_approval)."""
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    return "requires_approval" in {
        c["name"] for c in insp.get_columns("users")
    }


def _insert_user(email, *, full_name, is_superadmin):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    pw = generate_password_hash("x", method="pbkdf2:sha256")
    now = datetime.utcnow()
    if _users_has_requires_approval():
        db.session.execute(text(
            "INSERT INTO users (email, full_name, password_hash, "
            "locale, is_superadmin, is_active, status, "
            "email_verified_at, terms_version, "
            "failed_login_attempts, created_at, requires_approval) "
            "VALUES (:email, :full_name, :pw, 'ar', "
            ":is_super, 1, :status, :now, 'TEST', 0, :now, 0)"
        ), {"email": email, "full_name": full_name, "pw": pw,
             "is_super": 1 if is_superadmin else 0,
             "status": UserStatus.ACTIVE.value, "now": now})
        db.session.commit()
    else:
        u = User(email=email, password_hash=pw,
                 full_name=full_name, is_active=True,
                 is_superadmin=is_superadmin,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=now, terms_version="TEST")
        db.session.add(u)
        db.session.commit()
    return User.query.filter_by(email=email).first()


def _mk_super():
    return _insert_user(EMAIL_SUPER, full_name="cblk-super",
                         is_superadmin=True)


def _mk_regular_user():
    return _insert_user(EMAIL_USER, full_name="cblk-user",
                         is_superadmin=False)


def _client_as(user_id):
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

@check("1. Bulk soft-delete happy path (3 ids)")
def _():
    from app.models import Company, PlatformAuditLog
    _teardown()
    su = _mk_super()
    ids = [_mk_company(f"H1_{i}").id for i in range(3)]
    db.session.commit()
    data = _md([("company_id", str(i)) for i in ids]
                + [("reason", "test bulk")])
    r = _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=data, follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    db.session.expire_all()
    for cid in ids:
        c = db.session.get(Company, cid)
        assert c.deleted_at is not None, \
            f"row {cid} not soft-deleted"
        assert c.is_active is False
    pal = PlatformAuditLog.query.filter(
        PlatformAuditLog.action == "company_soft_delete",
        PlatformAuditLog.target_company_id.in_(ids),
    ).count()
    assert pal == 3, f"expected 3 PAL, got {pal}"
    return "3 soft-deleted + 3 audit lines"


@check("2. Bulk soft-delete with one bad id")
def _():
    from app.models import Company
    _teardown()
    su = _mk_super()
    good = [_mk_company(f"H2_{i}").id for i in range(2)]
    db.session.commit()
    bad = 9_999_999
    data = _md([("company_id", str(i)) for i in good + [bad]])
    r = _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=data, follow_redirects=False)
    assert r.status_code in (302, 303)
    db.session.expire_all()
    for cid in good:
        assert db.session.get(Company, cid).deleted_at is not None
    return "2 deleted, 1 skipped"


@check("3. Idempotency — second POST is a no-op")
def _():
    from app.models import PlatformAuditLog
    _teardown()
    su = _mk_super()
    cid = _mk_company("H3").id
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    n1 = PlatformAuditLog.query.filter_by(
        action="company_soft_delete",
        target_company_id=cid).count()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    n2 = PlatformAuditLog.query.filter_by(
        action="company_soft_delete",
        target_company_id=cid).count()
    assert n1 == 1 and n2 == 1, \
        f"duplicate PAL: n1={n1} n2={n2}"
    return "no duplicate audit line"


@check("4. Non-superadmin refused (403 on both bulk routes)")
def _():
    _teardown()
    _su = _mk_super()
    u = _mk_regular_user()
    cid = _mk_company("H4").id
    db.session.commit()
    r1 = _client_as(u.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    assert r1.status_code == 403, f"soft: {r1.status_code}"
    r2 = _client_as(u.id).post(
        "/admin/companies/bulk-hard-delete",
        data=_md([("company_id", str(cid)),
                   ("confirm_word", "تأكيد")]))
    assert r2.status_code == 403, f"hard: {r2.status_code}"
    return "both bulk routes 403 for non-superadmin"


@check("5. GET /admin/companies/deleted empty state → 200")
def _():
    _teardown()
    su = _mk_super()
    r = _client_as(su.id).get("/admin/companies/deleted")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "لا توجد شركات محذوفة" in body, \
        "empty state text missing"
    return "empty page renders"


@check("6. GET /admin/companies/deleted lists soft-deleted rows")
def _():
    _teardown()
    su = _mk_super()
    c = _mk_company("H6")
    cid, cname = c.id, c.name
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    r = _client_as(su.id).get("/admin/companies/deleted")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert cname in body, "company name not listed"
    return "listed"


@check("7. Main /admin/companies HIDES soft-deleted rows")
def _():
    _teardown()
    su = _mk_super()
    c_live = _mk_company("H7L")
    c_del = _mk_company("H7D")
    live_name, del_name = c_live.name, c_del.name
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(c_del.id))]))
    r = _client_as(su.id).get("/admin/companies")
    body = r.get_data(as_text=True)
    assert live_name in body, "live company missing from main list"
    assert del_name not in body, \
        "soft-deleted company leaked onto main list"
    return "filter hides deleted"


@check("8. Bulk hard-delete WITHOUT 'تأكيد' — refused")
def _():
    from app.models import Company
    _teardown()
    su = _mk_super()
    c = _mk_company("H8")
    cid = c.id
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    _clean_backup_files_for(cid)
    r = _client_as(su.id).post(
        "/admin/companies/bulk-hard-delete",
        data=_md([("company_id", str(cid)),
                   ("confirm_word", "wrong-word")]),
        follow_redirects=False)
    assert r.status_code in (302, 303)
    assert db.session.get(Company, cid) is not None, \
        "company was hard-deleted without 'تأكيد'!"
    d = _backup_dir()
    if d.exists():
        n_backups = sum(1 for f in d.iterdir()
                        if f.name.startswith(f"{cid}_"))
        assert n_backups == 0, \
            f"backup created without confirm ({n_backups})"
    return "confirm gate held"


@check("9. Bulk hard-delete WITH 'تأكيد' — wipes + backs up")
def _():
    from app.models import Company, PlatformAuditLog
    _teardown()
    su = _mk_super()
    c = _mk_company("H9")
    cid, cname = c.id, c.name
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    _clean_backup_files_for(cid)
    r = _client_as(su.id).post(
        "/admin/companies/bulk-hard-delete",
        data=_md([("company_id", str(cid)),
                   ("confirm_word", "تأكيد"),
                   ("reason", "audit hard purge")]),
        follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    db.session.expire_all()
    assert db.session.get(Company, cid) is None, \
        f"company {cname} still present after hard-delete"
    n = PlatformAuditLog.query.filter_by(
        action="company_hard_delete",
        target_company_id=cid).count()
    assert n >= 1, "no company_hard_delete PAL entry"
    d = _backup_dir()
    backups = [f for f in d.iterdir()
               if f.name.startswith(f"{cid}_")
               and f.suffix == ".json"]
    assert len(backups) == 1, \
        f"expected 1 backup file, got {len(backups)}"
    payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert payload["_manifest"]["company_id"] == cid
    assert payload["_manifest"]["company_name"] == cname
    assert "companies" in payload
    return f"wiped + backup ({backups[0].name})"


@check("10. Backup contains child-table rows")
def _():
    from app.models import Customer
    _teardown()
    su = _mk_super()
    c = _mk_company("H10")
    cid = c.id
    cu = Customer(company_id=cid, name="cblk-customer-1",
                   email="cblk-cust@x.test")
    db.session.add(cu)
    db.session.commit()
    _client_as(su.id).post(
        "/admin/companies/bulk-soft-delete",
        data=_md([("company_id", str(cid))]))
    _clean_backup_files_for(cid)
    _client_as(su.id).post(
        "/admin/companies/bulk-hard-delete",
        data=_md([("company_id", str(cid)),
                   ("confirm_word", "تأكيد")]))
    d = _backup_dir()
    backups = [f for f in d.iterdir()
               if f.name.startswith(f"{cid}_")
               and f.suffix == ".json"]
    assert backups, "no backup file created"
    payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert "customers" in payload, \
        f"customers not in backup keys: {list(payload)[:20]}"
    names = [row.get("name") for row in payload["customers"]]
    assert "cblk-customer-1" in names, \
        f"child customer missing from backup: {names}"
    return "1 customer row captured"


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
                print(f"FAIL  {label}  ⇒ "
                      f"{type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
