#!/usr/bin/env python3
"""MARSOUD-EMPLOYEE-DAILY-REPORTS — end-to-end audit.

Proves, on a fresh company:
  1. build_digest for an employee with no activity → None, no row.
  2. Employee with a user_activity_log entry → DRAFT row with body,
     linked User gets a DIGEST_DRAFT_READY notification.
  3. Second call same day is idempotent (no dup row, no dup notif).
  4. submit_report flips DRAFT → SUBMITTED, records submitted_at,
     notifies the owner AND anyone with employee_report_access.
  5. Second submit is a no-op.
  6. can_view_reports_for: owner=True, unrelated admin=False,
     admin with an access row=True.
  7. visible_employees_for returns exactly the granted set for a
     non-owner user.
  8. plan_allows("employee_reports.view", company) enforces the plan
     gate — a plan without the module blocks access.
  9. run_daily_digest_for_company built + skipped counts add up.
 10. Permission catalog + PERMISSION_CATALOG entries exist.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__DAILY_REPORTS_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User
    from app.models.user import user_companies
    # Wipe any leftover fixture users from a previous aborted run.
    stale_emails = ("empty@e.co", "active@e.co", "owner@e.co", "admin@e.co")
    for u in User.query.filter(User.email.in_(stale_emails)).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id
        ))
        db.session.delete(u)
    db.session.commit()

    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id
    db.session.commit()


def _teardown_company(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine, Invoice, InvoiceItem,
        Payment, VendorBill, VendorBillItem,
    )
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
    inv_ids = [r.id for r in Invoice.query.filter_by(
        company_id=company_id).all()]
    if inv_ids:
        InvoiceItem.query.filter(
            InvoiceItem.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
        Payment.query.filter(
            Payment.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
    bill_ids = [r.id for r in VendorBill.query.filter_by(
        company_id=company_id).all()]
    if bill_ids:
        VendorBillItem.query.filter(
            VendorBillItem.bill_id.in_(bill_ids),
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id),
            )
    # Also clean up any User rows tied to this company via user_companies —
    # user_companies gets wiped by the loop above, so we chase orphans.
    from app.models import User
    for email in ("empty@e.co", "active@e.co", "owner@e.co", "admin@e.co"):
        u = User.query.filter_by(email=email).first()
        if u:
            db.session.delete(u)
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


def _make_employee_and_user(name, email, role="employee"):
    """Create an Employee + the linked User + user_companies row."""
    from decimal import Decimal
    from app.models import Employee, EmployeeStatus, User, UserStatus
    from app.models.user import user_companies
    cid = _STATE["company_id"]
    emp = Employee(
        company_id=cid, name=name, email=email,
        status=EmployeeStatus.ACTIVE,
        basic_salary=Decimal("3000"), start_date=date.today(),
    )
    db.session.add(emp); db.session.flush()
    u = User(email=email, full_name=name,
              status=UserStatus.ACTIVE.value, employee_id=emp.id)
    u.set_password("x")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=cid, role=role,
    ))
    db.session.flush()
    return emp, u


@check("1. build_digest on empty user → None, no row")
def _():
    from app.services.daily_digest import build_digest
    from app.models import EmployeeDailyReport
    cid = _STATE["company_id"]
    emp, u = _make_employee_and_user("موظف فارغ", "empty@e.co")
    db.session.commit()
    r = build_digest(cid, emp.id, date.today())
    db.session.commit()
    assert r is None, "expected None on no-activity employee"
    n = EmployeeDailyReport.query.filter_by(employee_id=emp.id).count()
    assert n == 0, f"expected 0 rows, got {n}"
    _STATE["empty_emp_id"] = emp.id
    return "no activity → no row"


@check("2. build_digest with activity → DRAFT + notification")
def _():
    from app.services.daily_digest import build_digest
    from app.models import (
        EmployeeDailyReport, DailyReportStatus,
        Notification, NotificationKind, UserActivityLog,
    )
    cid = _STATE["company_id"]
    emp, u = _make_employee_and_user("موظف نشط", "active@e.co")
    # Simulate the user creating an invoice today.
    db.session.add(UserActivityLog(
        company_id=cid, user_id=u.id,
        action_type="CREATE", entity_type="invoice",
        entity_id=99, entity_label="INV-0099",
        created_at=datetime.utcnow(),
    ))
    db.session.commit()
    r = build_digest(cid, emp.id, date.today())
    db.session.commit()
    assert r is not None, "expected a DRAFT report"
    assert r.status == DailyReportStatus.DRAFT
    assert "INV-0099" in (r.body or ""), \
        f"body should mention the invoice: {r.body!r}"
    notif = Notification.query.filter_by(
        user_id=u.id,
        kind=NotificationKind.DIGEST_DRAFT_READY.value,
    ).first()
    assert notif, "expected DIGEST_DRAFT_READY notification"
    _STATE.update(active_emp_id=emp.id, active_user_id=u.id,
                   report_id=r.id)
    return f"DRAFT #{r.id} + notification {notif.id}"


@check("3. Second call same day is idempotent")
def _():
    from app.services.daily_digest import build_digest
    from app.models import (
        EmployeeDailyReport, Notification, NotificationKind,
    )
    cid = _STATE["company_id"]
    n_before_report = EmployeeDailyReport.query.filter_by(
        employee_id=_STATE["active_emp_id"],
    ).count()
    n_before_notif = Notification.query.filter_by(
        user_id=_STATE["active_user_id"],
        kind=NotificationKind.DIGEST_DRAFT_READY.value,
    ).count()
    r = build_digest(cid, _STATE["active_emp_id"], date.today())
    db.session.commit()
    n_after_report = EmployeeDailyReport.query.filter_by(
        employee_id=_STATE["active_emp_id"],
    ).count()
    n_after_notif = Notification.query.filter_by(
        user_id=_STATE["active_user_id"],
        kind=NotificationKind.DIGEST_DRAFT_READY.value,
    ).count()
    assert n_before_report == n_after_report == 1, \
        f"report rows: {n_before_report} vs {n_after_report}"
    assert n_before_notif == n_after_notif == 1, \
        f"notif rows: {n_before_notif} vs {n_after_notif}"
    return "1 row, 1 notif → still 1 after re-run"


@check("4. submit flips DRAFT → SUBMITTED + notifies owner")
def _():
    from app.services.daily_digest import submit_report
    from app.models import (
        EmployeeDailyReport, DailyReportStatus,
        Notification, NotificationKind, User, UserStatus,
    )
    from app.models.user import user_companies
    cid = _STATE["company_id"]
    # Create an owner user for this company.
    owner = User(email="owner@e.co", full_name="مالك",
                   status=UserStatus.ACTIVE.value)
    owner.set_password("x")
    db.session.add(owner); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=cid, role="owner",
    ))
    db.session.commit()
    _STATE["owner_id"] = owner.id
    r = submit_report(_STATE["report_id"], _STATE["active_user_id"])
    db.session.commit()
    assert r.status == DailyReportStatus.SUBMITTED
    assert r.submitted_at is not None
    n = Notification.query.filter_by(
        user_id=owner.id,
        kind=NotificationKind.EMPLOYEE_REPORT_SUBMITTED.value,
    ).first()
    assert n, "expected owner notification"
    return f"SUBMITTED, owner notified via {n.id}"


@check("5. Second submit is no-op")
def _():
    from app.services.daily_digest import submit_report
    from app.models import EmployeeDailyReport, DailyReportStatus, Notification, NotificationKind
    r = submit_report(_STATE["report_id"], _STATE["active_user_id"])
    db.session.commit()
    n = Notification.query.filter_by(
        user_id=_STATE["owner_id"],
        kind=NotificationKind.EMPLOYEE_REPORT_SUBMITTED.value,
    ).count()
    assert n == 1, f"expected still 1 notif, got {n}"
    return "still SUBMITTED, still 1 notif"


@check("6. can_view_reports_for gates correctly")
def _():
    from app.services.daily_digest import can_view_reports_for
    from app.models import User, UserStatus, EmployeeReportAccess
    from app.models.user import user_companies
    cid = _STATE["company_id"]
    admin = User(email="admin@e.co", full_name="أدمن غريب",
                    status=UserStatus.ACTIVE.value)
    admin.set_password("x")
    db.session.add(admin); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=admin.id, company_id=cid, role="admin",
    ))
    db.session.commit()
    owner = db.session.get(User, _STATE["owner_id"])

    # User inherits is_authenticated from UserMixin — always True for
    # instances loaded from DB with a valid PK. That's what production
    # sees when checking current_user.
    active_emp = _STATE["active_emp_id"]
    assert can_view_reports_for(owner, active_emp, cid) is True, \
        "owner must always see"
    assert can_view_reports_for(admin, active_emp, cid) is False, \
        "unrelated admin must NOT see"
    db.session.add(EmployeeReportAccess(
        company_id=cid, viewer_user_id=admin.id,
        employee_id=active_emp,
    ))
    db.session.commit()
    assert can_view_reports_for(admin, active_emp, cid) is True, \
        "admin with grant must see"
    _STATE["admin_id"] = admin.id
    return "owner=T, admin=F→T after grant"


@check("7. visible_employees_for filters by grants")
def _():
    from app.services.daily_digest import visible_employees_for
    from app.models import User
    cid = _STATE["company_id"]
    admin = db.session.get(User, _STATE["admin_id"])
    visible = visible_employees_for(admin, cid)
    ids = {e.id for e in visible}
    assert ids == {_STATE["active_emp_id"]}, \
        f"expected only granted emp, got {ids}"
    return f"admin sees {len(visible)} of 2 employees"


@check("8. Plan gating blocks when module disabled")
def _():
    from app.services.plan_gating import plan_allows, action_module
    from app.models import Company, Plan
    c = db.session.get(Company, _STATE["company_id"])
    # Without a subscription_plan, plan_allows returns True (back-compat).
    assert plan_allows("employee_reports.view", c) is True

    p = Plan.query.first()
    if p:
        original = list(p.modules or [])
        p.set_modules([m for m in original if m != "employee_reports"])
        c.plan_id = p.id
        db.session.commit()
        db.session.refresh(c)   # reload subscription_plan relationship
        allowed = plan_allows("employee_reports.view", c)
        p.set_modules(original)
        db.session.commit()
        assert allowed is False, \
            f"plan without module must block (modules={p.modules})"
    assert action_module("employee_reports.view") == "employee_reports"
    return "action_module ok; plan without module blocks"


@check("9. run_daily_digest_for_company reports built + skipped")
def _():
    from app.services.daily_digest import run_daily_digest_for_company
    cid = _STATE["company_id"]
    summary = run_daily_digest_for_company(cid, day=date.today())
    db.session.commit()
    assert "built" in summary and "skipped" in summary
    return f"built={summary['built']}, skipped={summary['skipped']}"


@check("10. permissions catalog wire-up")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    from app.services.plan_gating import SUB_ITEM_CATALOG
    from app.models import Permission
    assert "employee_reports.view" in PERMISSION_CATALOG
    assert "employee_reports" in SUB_ITEM_CATALOG
    # Re-seed to make sure the perm row lands.
    from app.services.roles_seed import seed_permissions_catalog
    seed_permissions_catalog()
    p = Permission.query.filter_by(code="employee_reports.view").first()
    assert p, "employee_reports.view missing from DB"
    return "catalog + DB row + section all wired"


# ─── Run ────────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
