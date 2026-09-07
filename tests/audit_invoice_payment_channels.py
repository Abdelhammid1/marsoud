#!/usr/bin/env python3
"""MARSOUD-INVOICE-PAYMENT-CHANNELS-01 (2026-09-08) — InstaPay +
e-wallet on the Company settings + invoice PDF.

Checks:
  1. Schema: three new columns on `companies`, all nullable.
  2. Settings POST persists all three; empty submits collapse to
     NULL (no whitespace ghost).
  3. PDF renders the InstaPay block when configured, hides it when
     NULL (Acceptance §3 — "لو الحقل فاضي، السطر ما يظهرش").
  4. PDF renders the e-wallet block with provider label when both
     configured; hides it when the number is NULL.
  5. Tenant with ALL channels empty produces a PDF that renders
     zero payment-channel blocks (baseline: existing behavior for
     any company that hasn't set anything).
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return owner.email, c.id, owner.id


def _make_invoice(cid):
    """Minimal Customer + Invoice for the PDF render tests."""
    from app import db
    from app.models import Customer, Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus
    from app.services.subsidiary import ensure_customer_account
    cust = Customer(company_id=cid, name="Client Ch")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id,
        number=f"CH-{cid}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=Decimal("0"),
        status=InvoiceStatus.SENT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="خدمة", quantity=Decimal("1"),
        unit_price=Decimal("100"),
        line_total=Decimal("100"),
    ))
    db.session.flush()
    inv.recalc()
    db.session.commit()
    return inv


@check("1. schema — three new columns on companies, all nullable")
def _():
    from app import create_app, db
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        cols = {c["name"]: c for c in inspect(
            db.engine).get_columns("companies")}
        for want in ("instapay_handle", "ewallet_number",
                     "ewallet_provider"):
            assert want in cols, f"missing column: {want}"
            assert cols[want]["nullable"] is True, \
                f"{want} must be nullable"
        return "3 nullable columns"


@check("2. settings POST persists + '' → NULL")
def _():
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CH2")
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        # Set values.
        r = client.post(f"/companies/{cid}/edit", data={
            "name": "Test",
            "instapay_handle": "owner@instapay",
            "ewallet_number": "01012345678",
            "ewallet_provider": "فودافون كاش",
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code
        db.session.expire_all()
        co = db.session.get(Company, cid)
        assert co.instapay_handle == "owner@instapay"
        assert co.ewallet_number == "01012345678"
        assert co.ewallet_provider == "فودافون كاش"
        # Clear them via blank submit.
        client.post(f"/companies/{cid}/edit", data={
            "name": "Test",
            "instapay_handle": "",
            "ewallet_number": "",
            "ewallet_provider": "",
        }, follow_redirects=True)
        db.session.expire_all()
        co = db.session.get(Company, cid)
        assert co.instapay_handle is None
        assert co.ewallet_number is None
        assert co.ewallet_provider is None
        return "set + clear both persist correctly"


@check("3. PDF renders InstaPay block when set; hides when NULL")
def _():
    from app import create_app, db
    from app.models import Company
    from flask import render_template
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CH3")
        inv = _make_invoice(cid)
        # Render WITHOUT InstaPay set — the "💠 InstaPay" line
        # must not appear at all.
        html_off = render_template(
            "pdfs/invoice.html", invoice=inv,
            company_logo_data_uri=None, amiri_font_face="",
            ar_date=lambda d: str(d),
        )
        assert "💠 InstaPay" not in html_off, \
            "InstaPay block leaked into a PDF that has no handle"
        # Set it and re-render.
        co = db.session.get(Company, cid)
        co.instapay_handle = "seller@instapay"
        db.session.commit()
        html_on = render_template(
            "pdfs/invoice.html", invoice=inv,
            company_logo_data_uri=None, amiri_font_face="",
            ar_date=lambda d: str(d),
        )
        assert "💠 InstaPay" in html_on
        assert "seller@instapay" in html_on
        return "InstaPay: shown when set, hidden when empty"


@check("4. PDF renders e-wallet with provider; hides when number "
        "is NULL")
def _():
    from app import create_app, db
    from app.models import Company
    from flask import render_template
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CH4")
        inv = _make_invoice(cid)
        co = db.session.get(Company, cid)
        # Number set + provider set.
        co.ewallet_number = "01099887766"
        co.ewallet_provider = "اتصالات كاش"
        db.session.commit()
        html_full = render_template(
            "pdfs/invoice.html", invoice=inv,
            company_logo_data_uri=None, amiri_font_face="",
            ar_date=lambda d: str(d),
        )
        assert "01099887766" in html_full
        assert "اتصالات كاش" in html_full
        assert "محفظة إلكترونية" in html_full
        # Clear number → whole block hides even if provider set.
        co.ewallet_number = None
        db.session.commit()
        html_off = render_template(
            "pdfs/invoice.html", invoice=inv,
            company_logo_data_uri=None, amiri_font_face="",
            ar_date=lambda d: str(d),
        )
        assert "01099887766" not in html_off
        assert "اتصالات كاش" not in html_off
        return "wallet: shown when configured, hidden when number empty"


@check("5. tenant with all channels empty → zero payment-channel "
        "blocks (baseline)")
def _():
    from app import create_app, db
    from flask import render_template
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CH5")
        inv = _make_invoice(cid)
        # Every channel column stays NULL (default).
        html = render_template(
            "pdfs/invoice.html", invoice=inv,
            company_logo_data_uri=None, amiri_font_face="",
            ar_date=lambda d: str(d),
        )
        for needle in ("💠 InstaPay", "محفظة إلكترونية",
                        "بيانات حسابي البنكي"):
            assert needle not in html, \
                f"leaked '{needle}' with all channels empty"
        return "clean baseline: no payment blocks when nothing set"


def main():
    from app import create_app
    _ = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
