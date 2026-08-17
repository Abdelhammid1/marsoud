#!/usr/bin/env python3
"""MARSOUD-VBILL-CURRENCY-DISPLAY (2026-08-17) — audit.

Verifies:
  A. `amount_ar` filter renders "1,234.50 <arabic name>" for known
     ISO codes, falls back to the raw code for unknown, and returns
     just the number when currency=None.
  B. Existing new-bill code path sets `bill.currency` to
     `active_company.base_currency` (regression guard — was already
     correct on `new_typed()`).
  C. `reports.py::late_vendor_bills` returns dicts with `currency`.
  D. Templates touched by this ticket no longer contain a bare
     `"%.2f"|format(bill.` cell — every amount goes through
     `amount_ar` now.
  E. The silent `except Exception: pass` in
     `reports.py::dashboard_metrics` around `late_vendor_bills` is
     gone (logs instead — never silent).
"""
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__VBILL_CUR_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, Vendor, VendorBill, VendorBillStatus
    from app.models.user import user_companies
    from app.services.legal import get_terms_version
    from datetime import datetime

    _teardown()

    tv = get_terms_version()
    now = datetime.utcnow()

    u = User(email=f"{CO_NAME.lower()}@x.local", full_name="cur audit",
             terms_version=tv, terms_accepted_at=now)
    u.set_password("Passw0rd!audit1"); db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_EGP", base_currency="EGP")
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    v = Vendor(company_id=co.id, name="Audit Vendor", is_active=True)
    db.session.add(v); db.session.flush()

    # Bill with matching base currency (EGP)
    b_egp = VendorBill(
        company_id=co.id, vendor_id=v.id, number="AUDIT-EGP-1",
        issue_date=date.today() - timedelta(days=30),
        due_date=date.today() - timedelta(days=5),  # overdue
        currency="EGP",
        total=Decimal("1234.50"), paid_amount=Decimal("0.00"),
        status=VendorBillStatus.POSTED,
    )
    # Bill with a different currency (SAR) — must be surfaced
    # correctly and not squished to EGP.
    b_sar = VendorBill(
        company_id=co.id, vendor_id=v.id, number="AUDIT-SAR-1",
        issue_date=date.today() - timedelta(days=30),
        due_date=date.today() - timedelta(days=2),  # overdue
        currency="SAR",
        total=Decimal("500.00"), paid_amount=Decimal("0.00"),
        status=VendorBillStatus.POSTED,
    )
    db.session.add_all([b_egp, b_sar])
    db.session.commit()

    _STATE["co"] = co
    _STATE["b_egp"] = b_egp
    _STATE["b_sar"] = b_sar


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


# ─── A. Filter ────────────────────────────────────────────────────────
@check("A1: amount_ar formats known ISO code with Arabic name")
def A1():
    app = _STATE["app"]
    f = app.jinja_env.filters["amount_ar"]
    assert f(1234.5, "EGP") == "1,234.50 جنيه مصري", f(1234.5, "EGP")
    assert f(1234.5, "SAR") == "1,234.50 ريال سعودي", f(1234.5, "SAR")
    assert f(1234.5, "sar") == "1,234.50 ريال سعودي", "case-insensitive"


@check("A2: amount_ar falls back to raw code for unknown currency")
def A2():
    app = _STATE["app"]
    f = app.jinja_env.filters["amount_ar"]
    assert f(100, "XYZ") == "100.00 XYZ", f(100, "XYZ")


@check("A3: amount_ar without currency returns just the number")
def A3():
    app = _STATE["app"]
    f = app.jinja_env.filters["amount_ar"]
    assert f(1234.5) == "1,234.50", f(1234.5)
    assert f(1234.5, None) == "1,234.50", f(1234.5, None)
    assert f(None, "EGP") == "", f(None, "EGP")


# ─── B. Backfill on creation ──────────────────────────────────────────
@check("B1: vendor_bills.new_typed sets bill.currency from active_company.base_currency")
def B1():
    # Source-level check — the runtime path goes through
    # `currency=g.active_company.base_currency` in vendor_bills.py:567.
    src = (ROOT / "app" / "routes" / "vendor_bills.py").read_text(
        encoding="utf-8")
    assert "currency=g.active_company.base_currency" in src, (
        "vendor_bills.new_typed no longer sets currency from active company")


# ─── C. late_vendor_bills carries currency ────────────────────────────
@check("C1: reports.late_vendor_bills dict rows include `currency` key")
def C1():
    from app.services.reports import dashboard_metrics
    co = _STATE["co"]
    m = dashboard_metrics(co.id, "month")
    rows = m.get("late_vendor_bills") or []
    assert rows, "expected at least the 2 fixture overdue bills"
    for r in rows:
        assert "currency" in r, f"row missing currency: {r}"
    # Assert the SAR bill carries SAR, not EGP.
    sar_rows = [r for r in rows if r.get("number") == "AUDIT-SAR-1"]
    assert sar_rows and sar_rows[0]["currency"] == "SAR", sar_rows


# ─── D. Templates no longer bare-format bill amounts ──────────────────
@check("D1: vendor_bills/index.html row cells go through amount_ar")
def D1():
    src = (ROOT / "app" / "templates" / "vendor_bills" / "index.html"
           ).read_text(encoding="utf-8")
    # These specific cells used to be bare `"%.2f"|format(b.total)` /
    # `b.balance`. Verify both now go through amount_ar.
    assert "b.total|amount_ar" in src, "index row total not through amount_ar"
    assert "b.balance|amount_ar" in src, "index row balance not through amount_ar"


@check("D2: vendor_bills/view.html total/subtotal/tax/paid/balance go through amount_ar")
def D2():
    src = (ROOT / "app" / "templates" / "vendor_bills" / "view.html"
           ).read_text(encoding="utf-8")
    for token in ["bill.total|amount_ar", "bill.subtotal|amount_ar",
                  "bill.tax_amount|amount_ar", "bill.balance|amount_ar"]:
        assert token in src, f"missing {token} in view.html"


@check("D3: dashboard overdue + upcoming row amounts go through amount_ar")
def D3():
    src = (ROOT / "app" / "templates" / "dashboard" / "index.html"
           ).read_text(encoding="utf-8")
    # The two per-row amount cells + the two totals must all use
    # amount_ar. Regex search — must appear at least 4 times.
    hits = re.findall(r"\|amount_ar\(", src)
    assert len(hits) >= 4, f"dashboard has only {len(hits)} amount_ar calls; expected ≥4"


# ─── E. Silent except: pass gone ──────────────────────────────────────
@check("E1: reports.py no longer swallows late_vendor_bills errors silently")
def E1():
    src = (ROOT / "app" / "services" / "reports.py").read_text(
        encoding="utf-8")
    # The historical footer was:
    #     except Exception:
    #         pass
    # right after the late_vendor_bills sort. It must log now.
    assert "late_vendor_bills failed" in src, (
        "expected a logger.exception(\"late_vendor_bills failed\")")


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
