#!/usr/bin/env python3
"""MARSOUD-VBILL-STATUS-VISIBILITY (2026-08-17) — TKT-D audit.

Verifies:
  A. `vendor_bill_bucket` returns the right bucket for each of
     the 6 canonical states.
  B. Cron-lag doesn't hide overdue: a POSTED bill past due_date
     buckets as `overdue` BEFORE the cron flip.
  C. `vendor_bills.view` permission is defined in P and reaches
     owner/admin/accountant/ceo/viewer; NOT sales_rep / hr /
     employee.
  D. `has_permission("vendor_bills.view")` returns True for
     someone with vendor_bills.create (via _IMPLIES).
  E. Dashboard metrics carry `due_today_vendor_bills` populated
     from a bill dated today.
  F. `/admin/vendor-bills/overdue` renders 200 as super-admin.
  G. `reports.py::late_vendor_bills` logs on error (not silent
     except: pass).
"""
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__VBILL_STATUS_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (Company, User, Vendor, VendorBill,
                             VendorBillStatus)
    from app.models.user import user_companies
    from app.services.legal import get_terms_version

    _teardown()
    now = datetime.utcnow()
    tv = get_terms_version()

    admin = User.query.filter_by(is_superadmin=True).first()

    u = User(email=f"{CO_NAME.lower()}@x.local", full_name="v",
             terms_version=tv, terms_accepted_at=now)
    u.set_password("Passw0rd!audit1"); db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_1", base_currency="EGP")
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    v = Vendor(company_id=co.id, name="Vendor A", is_active=True)
    db.session.add(v); db.session.flush()

    def _mk(number, status, due, total=100.0, paid=0.0, deleted=False):
        b = VendorBill(
            company_id=co.id, vendor_id=v.id, number=number,
            issue_date=due - timedelta(days=30),
            due_date=due,
            currency="EGP",
            total=Decimal(str(total)),
            paid_amount=Decimal(str(paid)),
            status=status,
        )
        if deleted:
            b.deleted_at = now
        db.session.add(b)
        return b

    today = date.today()
    b_upcoming = _mk("BST-UP", VendorBillStatus.POSTED, today + timedelta(days=5))
    b_due_today = _mk("BST-DT", VendorBillStatus.POSTED, today)
    b_overdue_posted = _mk("BST-OP", VendorBillStatus.POSTED, today - timedelta(days=5))
    b_overdue_flipped = _mk("BST-OF", VendorBillStatus.OVERDUE, today - timedelta(days=8))
    b_paid = _mk("BST-PD", VendorBillStatus.PAID, today - timedelta(days=15), total=100.0, paid=100.0)
    b_cancelled = _mk("BST-CN", VendorBillStatus.CANCELLED, today - timedelta(days=1))
    b_draft = _mk("BST-DR", VendorBillStatus.DRAFT, today + timedelta(days=3))
    b_deleted = _mk("BST-DL", VendorBillStatus.POSTED, today - timedelta(days=2), deleted=True)

    db.session.commit()

    _STATE["co"] = co
    _STATE["u_id"] = u.id
    _STATE["admin_id"] = admin.id if admin else None
    _STATE.update(dict(
        b_upcoming=b_upcoming, b_due_today=b_due_today,
        b_overdue_posted=b_overdue_posted,
        b_overdue_flipped=b_overdue_flipped,
        b_paid=b_paid, b_cancelled=b_cancelled,
        b_draft=b_draft, b_deleted=b_deleted,
    ))


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
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        db.session.execute(
            text("DELETE FROM companies WHERE id = :i"), {"i": cid})
    db.session.commit()


# ─── A. Bucket ─────────────────────────────────────────────────────────
@check("A1: vendor_bill_bucket returns correct bucket per state")
def A1():
    from app.services.vendor_bills import vendor_bill_bucket
    cases = [
        (_STATE["b_upcoming"], "upcoming"),
        (_STATE["b_due_today"], "due_today"),
        (_STATE["b_overdue_posted"], "overdue"),
        (_STATE["b_overdue_flipped"], "overdue"),
        (_STATE["b_paid"], "paid"),
        (_STATE["b_cancelled"], "cancelled"),
        (_STATE["b_draft"], "draft"),
        (_STATE["b_deleted"], "deleted"),
    ]
    for bill, expected in cases:
        got = vendor_bill_bucket(bill)
        assert got == expected, (
            f"{bill.number}: expected {expected}, got {got}")


# ─── B. Cron-lag doesn't hide overdue ─────────────────────────────────
@check("B1: POSTED past-due bill buckets as `overdue` BEFORE cron flip")
def B1():
    # b_overdue_posted has status=POSTED (cron hasn't flipped it)
    # but due_date is 5 days ago. Must bucket as overdue anyway.
    from app.services.vendor_bills import vendor_bill_bucket
    from app.models import VendorBillStatus
    b = _STATE["b_overdue_posted"]
    assert b.status == VendorBillStatus.POSTED, (
        "fixture drift — should still be POSTED")
    assert vendor_bill_bucket(b) == "overdue", (
        "cron lag would hide this row on the list")


# ─── C. Permission definition + reach ─────────────────────────────────
@check("C1: vendor_bills.view exists in P and reaches expected roles")
def C1():
    from app.services.permissions import P
    perms = P.get("vendor_bills.view")
    assert perms is not None, "vendor_bills.view missing from P"
    for role in ("owner", "admin", "accountant", "ceo", "viewer"):
        assert role in perms, f"{role} missing from vendor_bills.view"
    for role in ("sales_rep", "hr", "employee", "client"):
        assert role not in perms, (
            f"{role} should NOT have vendor_bills.view: {perms}")


# ─── D. _IMPLIES: vendor_bills.create ⇒ vendor_bills.view ─────────────
@check("D1: _IMPLIES grants vendor_bills.view to vendor_bills.create holders")
def D1():
    from app.services.permissions import _IMPLIES
    assert _IMPLIES.get("vendor_bills.view") == "vendor_bills.create", (
        f"_IMPLIES entry wrong: {_IMPLIES.get('vendor_bills.view')!r}")


# ─── E. Dashboard metrics carry due_today ─────────────────────────────
@check("E1: dashboard_metrics returns due_today_vendor_bills populated")
def E1():
    from app.services.reports import dashboard_metrics
    m = dashboard_metrics(_STATE["co"].id, "month")
    rows = m.get("due_today_vendor_bills")
    assert rows is not None, "due_today_vendor_bills key missing"
    numbers = {r["number"] for r in rows}
    assert "BST-DT" in numbers, (
        f"BST-DT (due today) missing from due_today list: {numbers}")
    assert m.get("due_today_vendor_bills_count") >= 1
    assert m.get("due_today_vendor_bills_total", 0) > 0


# ─── F. Super-admin cross-tenant panel renders ────────────────────────
@check("F1: /admin/vendor-bills/overdue renders 200 as super-admin")
def F1():
    from flask import g as flask_g
    admin_id = _STATE["admin_id"]
    if not admin_id:
        raise AssertionError("no super-admin in DB")
    if "_login_user" in flask_g:
        del flask_g._login_user
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    r = c.get("/admin/vendor-bills/overdue", follow_redirects=False)
    assert r.status_code == 200, (r.status_code,
                                    r.headers.get("Location"))
    body = r.data.decode("utf-8", errors="replace")
    # The fixture company should appear (has overdue bills)
    assert _STATE["co"].name in body, "fixture company missing"


@check("F2: overdue_vendor_bills_by_company includes the fixture bills")
def F2():
    from app.services.superadmin import overdue_vendor_bills_by_company
    rows = overdue_vendor_bills_by_company()
    co_id = _STATE["co"].id
    ours = [r for r in rows if r["company"].id == co_id]
    assert ours, "fixture company missing from cross-tenant list"
    bill_numbers = {b["number"] for b in ours[0]["bills"]}
    for expected in ("BST-OP", "BST-OF"):
        assert expected in bill_numbers, (
            f"{expected} missing from super-admin panel: {bill_numbers}")


# ─── G. reports.py logs on error ──────────────────────────────────────
@check("G1: late_vendor_bills logs on error (not silent)")
def G1():
    src = (ROOT / "app" / "services" / "reports.py").read_text(
        encoding="utf-8")
    assert "late_vendor_bills failed" in src, (
        "expected logger.exception(\"late_vendor_bills failed\")")


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
