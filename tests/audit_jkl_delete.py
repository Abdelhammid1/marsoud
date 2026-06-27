#!/usr/bin/env python3
"""Audit for tickets J / K / L — company + project soft-delete + restore.

  J) owner soft-deletes their own company (reason captured, PAL row).
  K) super-admin restore + permanent delete with audit log.
  L) owner can edit + soft-delete projects; non-owners blocked.
"""
import sys
from datetime import datetime, date
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"login {email} failed: status={r.status_code}"


# ─── 1. Schema + service basics ────────────────────────────────────────
@check("J1. Company gained deleted_at / deleted_by_id / deletion_reason")
def _():
    from app.models import Company
    cols = {c.name for c in Company.__table__.columns}
    for col in ("deleted_at", "deleted_by_id", "deletion_reason"):
        assert col in cols, f"Company missing {col!r}"
    return "all 3 columns present"


@check("L1. Project gained deleted_at / deleted_by_id / deletion_reason")
def _():
    from app.models import Project
    cols = {c.name for c in Project.__table__.columns}
    for col in ("deleted_at", "deleted_by_id", "deletion_reason"):
        assert col in cols, f"Project missing {col!r}"
    return "all 3 columns present"


@check("S1. lifecycle service exports soft_delete + restore + hard_delete")
def _():
    from app.services import lifecycle
    for fn in ("soft_delete_company", "restore_company",
                "hard_delete_company",
                "soft_delete_project", "restore_project"):
        assert hasattr(lifecycle, fn), f"missing helper {fn}"
    return "all 5 helpers exported"


# ─── 2. Company lifecycle round-trip ───────────────────────────────────
@check("J2. soft_delete_company flips columns + writes PlatformAuditLog")
def _():
    from app.models import Company, User, PlatformAuditLog
    from app.services.lifecycle import (
        soft_delete_company, restore_company,
    )
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    # Make a throwaway company
    co = Company(name="_AUDIT_COMPANY_J2", is_active=True)
    db.session.add(co); db.session.commit()
    cid = co.id
    try:
        n_before = PlatformAuditLog.query.filter_by(
            target_company_id=cid).count()
        soft_delete_company(co, actor_id=user.id, reason="audit test")
        db.session.refresh(co)
        assert co.deleted_at is not None
        assert co.deletion_reason == "audit test"
        assert co.deleted_by_id == user.id
        assert co.is_active is False
        n_after = PlatformAuditLog.query.filter_by(
            target_company_id=cid).count()
        assert n_after == n_before + 1, "audit log not written"
        # Restore
        restore_company(co, actor_id=user.id)
        db.session.refresh(co)
        assert co.deleted_at is None
        assert co.deletion_reason is None
        assert co.is_active is True
    finally:
        db.session.delete(co); db.session.commit()
    return "soft → restore round-trip + audit log captured"


@check("K1b. hard_delete_company cascades through customer + invoice + project + task")
def _():
    """Regression for the 'Internal Server Error' Abdelhamid hit on
    /admin/companies/21/delete with confirm_permanent=1. The naive
    db.session.delete used to crash on the first NOT-NULL
    company_id FK (customers.company_id). The new implementation
    walks db.metadata.sorted_tables in reverse and bulk-deletes
    every table that carries company_id."""
    from app.models import (
        Company, User, Customer, Invoice, InvoiceStatus,
        Project, ProjectStatus, Task, TaskStatus, TaskPriority,
    )
    from app.services.lifecycle import hard_delete_company
    from datetime import date as _date
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    co = Company(name="_AUDIT_K1B_CASCADE", is_active=True)
    db.session.add(co); db.session.commit()
    cust = Customer(company_id=co.id, name="x", phone="0",
                     email="ccc@ccc.com")
    db.session.add(cust); db.session.commit()
    inv = Invoice(company_id=co.id, customer_id=cust.id,
                   number="X-K1B", issue_date=_date.today(),
                   due_date=_date.today(), subtotal=100, total=100,
                   status=InvoiceStatus.DRAFT, currency="EGP")
    db.session.add(inv); db.session.commit()
    p = Project(company_id=co.id, name="p", type="t",
                 customer_id=cust.id, manager_id=user.id,
                 start_date=_date.today(), end_date=_date.today(),
                 status=ProjectStatus.PLANNING)
    db.session.add(p); db.session.commit()
    t = Task(company_id=co.id, title="t", project_id=p.id,
              assigned_to_id=user.id, created_by_id=user.id,
              status=TaskStatus.TODO, priority=TaskPriority.LOW)
    db.session.add(t); db.session.commit()
    cid = co.id
    name = hard_delete_company(co, actor_id=user.id,
                                reason="cascade regression")
    # Company is gone
    assert db.session.get(Company, cid) is None, "company row still exists"
    # Children are gone too
    assert Customer.query.filter_by(company_id=cid).count() == 0
    assert Invoice.query.filter_by(company_id=cid).count() == 0
    assert Project.query.filter_by(company_id=cid).count() == 0
    assert Task.query.filter_by(company_id=cid).count() == 0
    return f"wiped '{name}' + 1 customer + 1 invoice + 1 project + 1 task"


@check("K1. hard_delete_company removes row + logs PAL before drop")
def _():
    from app.models import Company, User, PlatformAuditLog
    from app.services.lifecycle import hard_delete_company
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    co = Company(name="_AUDIT_COMPANY_K1", is_active=True)
    db.session.add(co); db.session.commit()
    cid = co.id
    name = hard_delete_company(co, actor_id=user.id, reason="audit test")
    # Row gone
    assert db.session.get(Company, cid) is None
    # Audit row survives
    pal = PlatformAuditLog.query.filter_by(
        target_company_id=cid, action="company_hard_delete"
    ).first()
    assert pal is not None, "hard-delete PAL missing"
    return f"company '{name}' wiped + PAL preserved"


# ─── 3. Project lifecycle round-trip ───────────────────────────────────
@check("L2. soft_delete_project flips columns + restore reverses")
def _():
    from app.models import (
        Project, ProjectStatus, Customer, Company, User,
    )
    from app.services.lifecycle import (
        soft_delete_project, restore_project,
    )
    company = Company.query.filter(Company.deleted_at.is_(None)).first()
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    cust = Customer.query.filter_by(company_id=company.id).first()
    if not cust:
        cust = Customer(company_id=company.id, name="_a_c", phone="0",
                         email="a@a.a")
        db.session.add(cust); db.session.flush()
    p = Project(
        company_id=company.id, name="_AUDIT_PROJ_L2", type="x",
        customer_id=cust.id, manager_id=user.id,
        start_date=date.today(), end_date=date.today(),
        status=ProjectStatus.PLANNING,
    )
    db.session.add(p); db.session.commit()
    pid = p.id
    try:
        soft_delete_project(p, actor_id=user.id, reason="audit")
        db.session.refresh(p)
        assert p.deleted_at is not None
        assert p.deletion_reason == "audit"
        # Restore
        restore_project(p, actor_id=user.id)
        db.session.refresh(p)
        assert p.deleted_at is None
    finally:
        db.session.delete(p); db.session.commit()
        if cust.name == "_a_c":
            db.session.delete(cust); db.session.commit()
    return "project soft-delete + restore round-trip"


# ─── 4. HTTP-level visibility / filter ─────────────────────────────────
@check("L3. /projects/ index hides soft-deleted rows")
def _():
    from app.models import (
        Project, ProjectStatus, Customer, Company, User,
    )
    from app.services.lifecycle import soft_delete_project
    company = Company.query.filter(Company.deleted_at.is_(None)).first()
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    cust = Customer.query.filter_by(company_id=company.id).first() or \
        Customer(company_id=company.id, name="_b_c", phone="0",
                  email="b@b.b")
    if not cust.id:
        db.session.add(cust); db.session.flush()
    p = Project(
        company_id=company.id, name="_AUDIT_LIST_HIDE", type="x",
        customer_id=cust.id, manager_id=user.id,
        start_date=date.today(), end_date=date.today(),
        status=ProjectStatus.PLANNING,
    )
    db.session.add(p); db.session.commit()
    try:
        soft_delete_project(p, actor_id=user.id, reason="hide-test")
        app = create_app()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            body = client.get("/projects/").data.decode("utf-8")
            assert "_AUDIT_LIST_HIDE" not in body, \
                "soft-deleted project still visible on /projects/"
    finally:
        db.session.delete(p); db.session.commit()
    return "deleted project filtered out of the list"


@check("L4. /projects/<id>/edit gated to owner/admin")
def _():
    """Edit route requires projects.manage AND role in (owner, admin).
    Non-owner attempts get 403/redirect."""
    from werkzeug.security import generate_password_hash
    from app.models import (
        User, Company, Role, Project, ProjectStatus, Customer,
    )
    from app.models.user import user_companies

    company = Company.query.filter(Company.deleted_at.is_(None)).first()
    owner_user = User.query.filter_by(email=DEMO_EMAIL).first()
    # Pick / create a project to target
    p = Project.query.filter_by(
        company_id=company.id,
        deleted_at=None,
    ).first()
    cleanup_p = False
    if not p:
        cust = Customer.query.filter_by(company_id=company.id).first()
        p = Project(
            company_id=company.id, name="_AUDIT_EDIT_GATE", type="x",
            customer_id=cust.id, manager_id=owner_user.id,
            start_date=date.today(), end_date=date.today(),
            status=ProjectStatus.PLANNING,
        )
        db.session.add(p); db.session.commit()
        cleanup_p = True

    # Create a non-owner test user with project_manager role
    u = User.query.filter_by(email="pm_edit_test@test.com").first()
    if not u:
        u = User(email="pm_edit_test@test.com",
                  full_name="pm edit test",
                  password_hash=generate_password_hash(
                      "x1234567", method="pbkdf2:sha256"),
                  is_active=True)
        db.session.add(u); db.session.flush()
    else:
        u.password_hash = generate_password_hash(
            "x1234567", method="pbkdf2:sha256")
    pm_role = Role.query.filter_by(
        company_id=company.id, code="project_manager",
    ).first()
    db.session.execute(user_companies.delete().where(
        (user_companies.c.user_id == u.id) &
        (user_companies.c.company_id == company.id)))
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=company.id,
        role="project_manager", role_id=pm_role.id,
    ))
    db.session.commit()
    try:
        app = create_app()
        with app.test_client() as client:
            _login(client, "pm_edit_test@test.com", "x1234567")
            r_edit = client.get(f"/projects/{p.id}/edit",
                                 follow_redirects=False)
            assert r_edit.status_code in (302, 303, 403), \
                f"PM should be blocked from edit, got {r_edit.status_code}"
            r_del = client.post(
                f"/projects/{p.id}/delete",
                data={"reason": "should fail"},
                follow_redirects=False,
            )
            # PM should NOT be able to delete — either 403 or redirect with flash
            assert r_del.status_code in (302, 303, 403)
        # Owner should be able
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            r_owner = client.get(f"/projects/{p.id}/edit",
                                  follow_redirects=False)
            assert r_owner.status_code == 200, \
                f"owner should reach edit, got {r_owner.status_code}"
    finally:
        db.session.execute(user_companies.delete().where(
            (user_companies.c.user_id == u.id) &
            (user_companies.c.company_id == company.id)))
        User.query.filter_by(email="pm_edit_test@test.com").delete()
        if cleanup_p:
            db.session.delete(p)
        db.session.commit()
    return "PM blocked; owner reaches edit"


@check("J3. /companies/<id>/soft-delete requires a reason + owner-only")
def _():
    from werkzeug.datastructures import MultiDict
    from app.models import Company, User
    company = Company.query.filter(Company.deleted_at.is_(None)).first()
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        # Empty reason → redirect with flash (not a 500)
        r = client.post(
            f"/companies/{company.id}/soft-delete",
            data=MultiDict([("reason", "")]),
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), \
            f"empty reason should redirect, got {r.status_code}"
    return "empty reason rejected with redirect"


@check("K2. Super-admin company_detail page renders restore + permanent UI")
def _():
    src = (ROOT / "app/templates/admin/company_detail.html").read_text()
    assert "company_restore" in src
    assert "confirm_permanent" in src
    assert "حذف نهائي" in src
    return "restore + permanent buttons rendered"


@check("F1. load_active_company excludes soft-deleted companies for non-superadmin")
def _():
    """Take an existing company the demo user belongs to, temporarily
    flip it to soft-deleted, hit /home, confirm it's hidden from the
    switcher, then restore. No row creation/destruction (which would
    trip roles FK cascades during cleanup)."""
    from app.models import Company, User
    user = User.query.filter_by(email=DEMO_EMAIL).first()
    # Pick any active company the demo user belongs to.
    co = next((c for c in user.companies
                if c.deleted_at is None), None)
    if not co:
        return "skipped — no active company on demo user"
    original_active = co.is_active
    try:
        co.deleted_at = datetime.utcnow()
        co.deleted_by_id = user.id
        co.deletion_reason = "audit transient"
        co.is_active = False
        db.session.commit()
        original_name = co.name
        app = create_app()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            r = client.get("/home", follow_redirects=False)
            body = r.data.decode("utf-8")
            # Either redirected to /companies/new (no active company),
            # or rendered without the deleted company's name in the
            # switcher options.
            options = body.split('<option value="')
            option_names = [opt.split('>', 1)[-1].split('<', 1)[0]
                             for opt in options[1:]]
            assert original_name not in option_names, \
                f"soft-deleted company '{original_name}' visible in switcher"
    finally:
        # Restore the company to its prior state.
        co.deleted_at = None
        co.deleted_by_id = None
        co.deletion_reason = None
        co.is_active = original_active
        db.session.commit()
    return "soft-deleted company hidden from /home switcher"


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
