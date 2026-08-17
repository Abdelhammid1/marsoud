#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-USER-360 — audit for /admin/users/<id>.

Verifies:
  A. user_snapshot() returns the expected shape
  B. Route returns 200 for a real user, 404 for a missing id,
     403 for a non-superadmin
  C. Snapshot for a user with N companies contains N companies
  D. Snapshot for a user with 0 companies has empty lists (no error)
  E. admin/users.html links each user's name to /admin/users/<id>
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__USER_360_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.legal import get_terms_version

    _teardown()

    plan_growth = Plan.query.filter_by(code="growth").first()
    admin = User.query.filter_by(is_superadmin=True).first()
    tv = get_terms_version()
    now = datetime.utcnow()

    def _mk_user(email, is_active=True):
        u = User(email=email, full_name=f"AuditUser {email}",
                 is_active=is_active, terms_version=tv,
                 terms_accepted_at=now)
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        return u

    def _mk_co(name, **kw):
        c = Company(name=name, base_currency="EGP", **kw)
        db.session.add(c); db.session.flush()
        return c

    def _link(u, c, role="owner"):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role=role))

    # 1. User with 2 companies
    u_multi = _mk_user(f"{CO_NAME.lower()}_multi@x.local")
    co_a = _mk_co(f"{CO_NAME}_multi_A", plan_id=plan_growth.id,
                  subscription_started_at=now,
                  subscription_expires_at=now + timedelta(days=10))
    co_b = _mk_co(f"{CO_NAME}_multi_B")
    _link(u_multi, co_a, role="owner")
    _link(u_multi, co_b, role="accountant")

    # 2. User with 0 companies (orphan)
    u_orphan = _mk_user(f"{CO_NAME.lower()}_orphan@x.local")

    # 3. Plain employee (non-superadmin) for the 403 test
    u_plain = _mk_user(f"{CO_NAME.lower()}_plain@x.local")
    co_p = _mk_co(f"{CO_NAME}_plain")
    _link(u_plain, co_p, role="employee")

    db.session.commit()
    _STATE["u_multi"] = u_multi
    _STATE["u_orphan"] = u_orphan
    _STATE["u_plain"] = u_plain
    _STATE["admin_id"] = admin.id if admin else None


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User
    from app.models.user import user_companies

    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                db.session.execute(
                    t.delete().where(t.c.company_id.in_(ids)))
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}_%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        for t in reversed(db.metadata.sorted_tables):
            if "user_id" in t.c and t.name != "user_companies":
                db.session.execute(t.delete().where(t.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        db.session.execute(
            text("DELETE FROM companies WHERE id = :i"), {"i": cid})
    db.session.commit()


# ─── A. Snapshot shape ────────────────────────────────────────────────
@check("A1: user_snapshot(missing_id) returns None")
def A1():
    from app.services.user_360 import user_snapshot
    assert user_snapshot(99_999_999) is None


@check("A2: user_snapshot returns all six sub-collections + user")
def A2():
    from app.services.user_360 import user_snapshot
    snap = user_snapshot(_STATE["u_multi"].id)
    for k in ("user", "companies", "roles_per_company",
              "activity", "sessions", "invitations_received",
              "invitations_sent", "consent_events",
              "company_count", "login_count"):
        assert k in snap, f"missing key: {k}"


# ─── C. Multi-company user ────────────────────────────────────────────
@check("C1: multi-company user snapshot lists all its companies")
def C1():
    from app.services.user_360 import user_snapshot
    snap = user_snapshot(_STATE["u_multi"].id)
    assert snap["company_count"] == 2, snap["company_count"]
    names = sorted(c["company"].name for c in snap["companies"])
    assert names == sorted([
        f"{CO_NAME}_multi_A", f"{CO_NAME}_multi_B"]), names
    roles = sorted(c["role_code"] for c in snap["companies"])
    assert roles == ["accountant", "owner"], roles


@check("C2: per-company plan snapshot matches plan_snapshot() alone")
def C2():
    from app.services.plan_snapshot import plan_snapshot
    from app.services.user_360 import user_snapshot
    snap = user_snapshot(_STATE["u_multi"].id)
    for c in snap["companies"]:
        expected = plan_snapshot(c["company"])
        got = c["plan_snapshot"]
        assert got["plan_code"] == expected["plan_code"], (got, expected)
        assert got["status"] == expected["status"], (got, expected)


# ─── D. Zero-company user doesn't crash ───────────────────────────────
@check("D1: orphan user (0 companies) snapshot renders as empty lists")
def D1():
    from app.services.user_360 import user_snapshot
    snap = user_snapshot(_STATE["u_orphan"].id)
    assert snap["company_count"] == 0, snap
    assert snap["companies"] == [], snap
    assert snap["roles_per_company"] == [], snap
    # No exceptions accessing anything else
    assert snap["activity"] == [], snap
    assert snap["sessions"] == [], snap


# ─── B. Route access control + rendering ──────────────────────────────
@check("B1: /admin/users/<id> renders 200 as super-admin")
def B1():
    admin_id = _STATE["admin_id"]
    if not admin_id:
        raise AssertionError("no super-admin in DB")
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    r = c.get(f"/admin/users/{_STATE['u_multi'].id}")
    assert r.status_code == 200, r.status_code
    body = r.data.decode("utf-8", errors="replace")
    # Both linked company names should render
    assert f"{CO_NAME}_multi_A" in body, "company A missing from render"
    assert f"{CO_NAME}_multi_B" in body, "company B missing from render"
    # Tab-section headings
    assert "الشركات المرتبطة" in body
    assert "الأدوار والصلاحيات" in body
    assert "سجل النشاط" in body
    assert "سجل الدخول" in body


@check("B2: /admin/users/<missing> returns 404 as super-admin")
def B2():
    admin_id = _STATE["admin_id"]
    if not admin_id:
        raise AssertionError("no super-admin in DB")
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    r = c.get("/admin/users/99999999")
    assert r.status_code == 404, r.status_code


@check("B3: orphan user (0 companies) page renders with empty state")
def B3():
    admin_id = _STATE["admin_id"]
    if not admin_id:
        raise AssertionError("no super-admin in DB")
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    r = c.get(f"/admin/users/{_STATE['u_orphan'].id}")
    assert r.status_code == 200, r.status_code
    body = r.data.decode("utf-8", errors="replace")
    assert "لا توجد شركات مرتبطة" in body, (
        "empty-state message missing")


# ─── E. users.html links to the detail page ───────────────────────────
@check("E1: admin/users.html links user's name to /admin/users/<id>")
def E1():
    src = (ROOT / "app" / "templates" / "admin" / "users.html").read_text(
        encoding="utf-8")
    assert "superadmin.user_detail" in src, (
        "users.html doesn't call url_for('superadmin.user_detail')")


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
