#!/usr/bin/env python3
"""MARSOUD-COST-CENTERS-03-REVENUE-SPLIT (2026-09-03).

The single aggregate revenue credit in post_invoice_to_ledger()
now splits into one line per distinct InvoiceItem.cost_center_id
bucket. This audit proves:

  1. Single-item invoice with no CC — JE unchanged (safety net;
     covers every historic + every POS invoice).
  2. Two items with different CCs → two revenue lines, one per CC.
  3. Three items collapse into buckets (same CC accumulates).
  4. Invoice-level FIXED discount splits pro-rata + Σ credits ==
     taxable_base to the cent (proves loyalty redemption is also
     covered, since it uses invoice_discount_type=FIXED).
  5. Cross-tenant CC id via POST is silently dropped to None by
     _pick_cc_at — no revenue leaks into another tenant's bucket.
  6. /reports/cost-centers renders the split totals (revenue
     column starts reading real numbers, not zero).

Base scaffolding copied from tests/audit_cost_centers.py:_boot
via tests/audit_cost_centers_expense_coverage.py so this audit is
self-contained.
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
    """Company + owner + seeded CoA. Wipes any residue from a prior
    run. Duplicated from tests/audit_cost_centers.py:_boot per
    codebase per-audit style."""
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
    db.session.execute(text(
        "DELETE FROM journal_entries WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN (SELECT id FROM journal_entries)"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports"])
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


def _make_cc(cid, code, name):
    from app import db
    from app.models import CostCenter
    cc = CostCenter(company_id=cid, code=code, name=name,
                     is_active=True)
    db.session.add(cc); db.session.commit()
    return cc


def _make_customer(cid, name="Client"):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    c = Customer(company_id=cid, name=name)
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.commit()
    return c


_INV_COUNTER = [0]


def _make_invoice(cid, cust, item_specs, *, tax_rate=0,
                    invoice_discount_type=None,
                    invoice_discount_value=0):
    """Build an untracked-services Invoice with items and recalc it.
    item_specs = list of (unit_price, cc_id_or_None) tuples; qty=1
    each so line_total == unit_price and taxable_base math is easy
    to reason about in assertions."""
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus, DiscountType
    _INV_COUNTER[0] += 1
    inv = Invoice(
        company_id=cid,
        customer_id=cust.id,
        number=f"AUD-CC03-{cid}-{_INV_COUNTER[0]}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=Decimal(str(tax_rate)),
        invoice_discount_type=(
            DiscountType[invoice_discount_type]
            if invoice_discount_type else DiscountType.NONE),
        invoice_discount_value=Decimal(str(invoice_discount_value)),
        status=InvoiceStatus.DRAFT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    for i, (price, cc_id) in enumerate(item_specs):
        it = InvoiceItem(
            invoice_id=inv.id, company_id=cid,
            description=f"بند {i+1}",
            quantity=Decimal("1"),
            unit_price=Decimal(str(price)),
            line_total=Decimal(str(price)),
            cost_center_id=cc_id,
        )
        db.session.add(it)
    db.session.flush()
    inv.recalc()
    db.session.commit()
    return inv


def _revenue_lines(entry):
    """Return the JournalLines credited to the seeded 4100 revenue
    account. Iterated in id order so tests can assert on the LAST-
    bucket-carries-residue rule."""
    from app import db
    from app.models import JournalLine, Account
    from flask import g as _g  # not used, just avoid stale ref
    rev = Account.query.filter_by(
        company_id=entry.company_id, code="4100").first()
    return (JournalLine.query
            .filter_by(entry_id=entry.id, account_id=rev.id)
            .order_by(JournalLine.id).all())


@check("1. Single-item invoice, no CC → 1 revenue line (JE shape "
        "byte-identical to pre-ticket)")
def _():
    from app import create_app, db
    from app.models import JournalLine
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC03A")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust, [(100.0, None)])
        entry = post_invoice_to_ledger(inv, created_by=oid)
        rev_lines = _revenue_lines(entry)
        assert len(rev_lines) == 1, \
            f"expected 1 revenue line, got {len(rev_lines)}"
        assert rev_lines[0].cost_center_id is None
        assert abs(float(rev_lines[0].credit) - 100.0) < 0.005
        # Whole JE: AR + Revenue only (tax=0).
        all_lines = JournalLine.query.filter_by(
            entry_id=entry.id).all()
        assert len(all_lines) == 2, f"total lines={len(all_lines)}"
        return "1 revenue line, cost_center_id NULL, credit=100.00"


@check("2. Two items, one CC each → two revenue lines")
def _():
    from app import create_app, db
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC03B")
        cust = _make_customer(cid)
        cc_a = _make_cc(cid, "A", "Branch A")
        cc_b = _make_cc(cid, "B", "Branch B")
        inv = _make_invoice(cid, cust, [
            (100.0, cc_a.id),
            (200.0, cc_b.id),
        ])
        entry = post_invoice_to_ledger(inv, created_by=oid)
        rev_lines = _revenue_lines(entry)
        assert len(rev_lines) == 2, \
            f"expected 2 revenue lines, got {len(rev_lines)}"
        by_cc = {l.cost_center_id: float(l.credit) for l in rev_lines}
        assert abs(by_cc[cc_a.id] - 100.0) < 0.005, by_cc
        assert abs(by_cc[cc_b.id] - 200.0) < 0.005, by_cc
        assert abs(sum(by_cc.values()) - 300.0) < 0.005
        return f"CC-A=100.00, CC-B=200.00, Σ=300.00"


@check("3. Three items collapse into buckets by CC (A+A groups)")
def _():
    from app import create_app, db
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC03C")
        cust = _make_customer(cid)
        cc_a = _make_cc(cid, "A", "A")
        inv = _make_invoice(cid, cust, [
            (60.0, cc_a.id),
            (40.0, cc_a.id),
            (100.0, None),
        ])
        entry = post_invoice_to_ledger(inv, created_by=oid)
        rev_lines = _revenue_lines(entry)
        assert len(rev_lines) == 2, \
            f"expected 2 buckets, got {len(rev_lines)}"
        by_cc = {l.cost_center_id: float(l.credit) for l in rev_lines}
        assert abs(by_cc[cc_a.id] - 100.0) < 0.005, by_cc
        assert abs(by_cc[None] - 100.0) < 0.005, by_cc
        return "CC-A collapsed 60+40=100; NULL bucket=100"


@check("4. Invoice-level FIXED discount → pro-rata split, "
        "Σ credits == taxable_base to the cent")
def _():
    from app import create_app, db
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC03D")
        cust = _make_customer(cid)
        cc_a = _make_cc(cid, "A", "A")
        cc_b = _make_cc(cid, "B", "B")
        # Odd figures on purpose so the rounding-residue logic gets
        # exercised. 33.33 + 66.67 = 100, -10 discount → base 90.
        inv = _make_invoice(cid, cust, [
            (33.33, cc_a.id),
            (66.67, cc_b.id),
        ], invoice_discount_type="FIXED", invoice_discount_value=10)
        db.session.refresh(inv)
        assert abs(float(inv.taxable_base) - 90.0) < 0.005, \
            f"taxable_base={inv.taxable_base}"
        entry = post_invoice_to_ledger(inv, created_by=oid)
        rev_lines = _revenue_lines(entry)
        assert len(rev_lines) == 2
        by_cc = {l.cost_center_id: float(l.credit) for l in rev_lines}
        # Pro-rata: A gets 33.33/100 of 90 = 29.997 → rounds to 30.00.
        # B is the last bucket, absorbs residue: 90 - 30.00 = 60.00.
        assert abs(by_cc[cc_a.id] - 30.00) < 0.005, by_cc
        assert abs(by_cc[cc_b.id] - 60.00) < 0.005, by_cc
        # Penny-perfect sum invariant — the whole point of the
        # last-bucket residue rule.
        assert round(sum(by_cc.values()), 2) \
                == round(float(inv.taxable_base), 2)
        return f"A=30.00 B=60.00 Σ=90.00 == taxable_base"


@check("5. Cross-tenant CC id via HTTP POST → silently dropped to "
        "NULL; revenue lands in the unclassified bucket")
def _():
    from app import create_app, db
    from app.models import Invoice, InvoiceStatus
    app = create_app()
    with app.app_context():
        # Tenant A + own CC.
        email_a, cid_a, oid_a = _boot("CC03EA")
        cust_a = _make_customer(cid_a, "Client-A")
        _cc_a = _make_cc(cid_a, "A", "own")
        # Tenant B + its CC (whose id we'll attempt to inject).
        email_b, cid_b, oid_b = _boot("CC03EB")
        _cust_b = _make_customer(cid_b, "Client-B")
        cc_b = _make_cc(cid_b, "B", "foreign")
        # Log into tenant A as the owner.
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid_a)
            sess["_fresh"] = True
            sess["active_company_id"] = cid_a
        # POST an invoice into tenant A with an item pointing at
        # tenant B's CC id. Route sends it → _pick_cc_at drops it →
        # InvoiceItem.cost_center_id == None.
        r = client.post("/invoices/new", data={
            "customer_id": str(cust_a.id),
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
            "tax_rate": "0",
            "invoice_discount_type": "NONE",
            "invoice_discount_value": "0",
            "notes": "", "internal_notes": "",
            "item_description[]": "بند اختبار",
            "item_quantity[]": "1",
            "item_unit_price[]": "50",
            "item_discount_type[]": "NONE",
            "item_discount_value[]": "0",
            "item_cost_center_id[]": str(cc_b.id),
            # Do NOT send=1 — we want the DRAFT to inspect.
        })
        # Route redirects on success (either to detail or back to
        # index on error). Either way, the invoice row should have
        # been persisted before any redirect.
        assert r.status_code in (200, 302), (
            f"got {r.status_code}: {r.get_data(as_text=True)[:200]}")
        inv = Invoice.query.filter_by(
            company_id=cid_a, customer_id=cust_a.id
        ).order_by(Invoice.id.desc()).first()
        assert inv is not None, "invoice not created"
        assert len(inv.items) == 1
        assert inv.items[0].cost_center_id is None, (
            f"cross-tenant id leaked through: got "
            f"{inv.items[0].cost_center_id}")
        return "cross-tenant CC id dropped to NULL as expected"


@check("6. /reports/cost-centers surfaces the split revenue after "
        "post_invoice_to_ledger")
def _():
    from app import create_app, db
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC03F")
        cust = _make_customer(cid)
        cc_a = _make_cc(cid, "AAA", "Branch A")
        cc_b = _make_cc(cid, "BBB", "Branch B")
        inv = _make_invoice(cid, cust, [
            (150.0, cc_a.id),
            (250.0, cc_b.id),
        ])
        post_invoice_to_ledger(inv, created_by=oid)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        r = client.get("/reports/cost-centers")
        assert r.status_code == 200, (
            f"got {r.status_code}: {r.get_data(as_text=True)[:200]}")
        html = r.get_data(as_text=True)
        assert "Branch A" in html and "Branch B" in html, \
            "CC names missing from report"
        # Split figures must appear as classified revenue.
        assert "150.00" in html, "CC-A revenue 150.00 missing"
        assert "250.00" in html, "CC-B revenue 250.00 missing"
        return "report shows CC-A=150.00, CC-B=250.00 revenue"


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
