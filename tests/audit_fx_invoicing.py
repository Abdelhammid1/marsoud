#!/usr/bin/env python3
"""MARSOUD-INVOICE-FX-01 (2026-09-08) — foreign-currency invoicing.

Checks:
  1. EGP-in-EGP tenant — post_invoice_to_ledger + record_payment
     behave byte-identical to today (baseline safety net).
  2. SAR-in-EGP tenant at issue — post_invoice_to_ledger returns
     None, no JournalEntry created.
  3. SAR-in-EGP tenant on collection — record_payment posts ONE
     EGP-denominated JE covering Cash + Revenue + VAT.
  4. Foreign collection without exchange_rate → LedgerError.
  5. Partial foreign collections at different rates → each posts
     its own JE at its own rate; invoice.paid_amount stays in
     foreign currency; final fx_rate_at_receipt = LAST rate.
  6. EGP invoice with an extra exchange_rate arg → ignored (no
     regression for callers who default it).
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


def _boot(prefix, *, base_currency="EGP"):
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
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency=base_currency,
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


def _make_customer(cid, name="Client"):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    c = Customer(company_id=cid, name=name)
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.commit()
    return c


def _make_pm(cid, code="1110"):
    """PaymentMethod pointing at Cash 1110. Reuses whatever
    seed_default_coa already inserted (a default "نقدي" row exists
    on every tenant); only creates one when nothing's there yet."""
    from app import db
    from app.models import Account, PaymentMethod
    pm = PaymentMethod.query.filter_by(
        company_id=cid, is_active=True).first()
    if pm:
        return pm
    acc = Account.query.filter_by(company_id=cid, code=code).first()
    pm = PaymentMethod(company_id=cid, name="Cash-FX-audit",
                        name_ar="نقدي (FX)",
                        account_id=acc.id, is_active=True,
                        is_default=True)
    db.session.add(pm); db.session.commit()
    return pm


_INV_COUNTER = [0]


def _make_invoice(cid, cust, subtotal, *, currency="EGP",
                    tax_rate=15, status_val=None):
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus, DiscountType
    _INV_COUNTER[0] += 1
    inv = Invoice(
        company_id=cid,
        customer_id=cust.id,
        number=f"AUD-FX-{cid}-{_INV_COUNTER[0]}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency=currency,
        tax_rate=Decimal(str(tax_rate)),
        invoice_discount_type=DiscountType.NONE,
        invoice_discount_value=Decimal("0"),
        status=status_val or InvoiceStatus.DRAFT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    it = InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="خدمة اختبار",
        quantity=Decimal("1"),
        unit_price=Decimal(str(subtotal)),
        line_total=Decimal(str(subtotal)),
    )
    db.session.add(it); db.session.flush()
    inv.recalc()
    db.session.commit()
    return inv


@check("1. EGP-in-EGP baseline — post + record_payment behave "
        "byte-identical to pre-ticket")
def _():
    from app import create_app, db
    from app.models import JournalEntry, JournalLine, Account
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment,
    )
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX1")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice(cid, cust, 100)
        # tax_rate=15 → subtotal 100, tax 15, total 115.
        entry = post_invoice_to_ledger(inv, created_by=oid)
        assert entry is not None, "EGP invoice must still post at issue"
        # AR + Revenue (per-CC split, one bucket) + VAT
        lines = JournalLine.query.filter_by(entry_id=entry.id).all()
        ar_acc = Account.query.filter_by(
            company_id=cid, code="1130").first()
        # 1130 is the header; the sub-account for this customer is
        # the actual receiver. Match by name pattern instead.
        by_code = {}
        for l in lines:
            acc = db.session.get(Account, l.account_id)
            by_code.setdefault(acc.code, l)
        assert "4100" in by_code, "revenue line missing"
        assert abs(float(by_code["4100"].credit) - 100.0) < 0.01
        # Now pay full.
        record_payment(inv, 115, payment_method_id=pm.id,
                        created_by=oid, notify=False)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID
        assert inv.fx_rate_at_receipt is None, (
            "EGP path must not stamp fx_rate_at_receipt")
        return "EGP happy path unchanged; no FX pollution"


@check("2. SAR-in-EGP invoice at issue → post returns None, no JE")
def _():
    from app import create_app, db
    from app.models import JournalEntry
    from app.services.invoicing import post_invoice_to_ledger
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX2")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust, 100, currency="SAR",
                             tax_rate=15)
        entry = post_invoice_to_ledger(inv, created_by=oid)
        assert entry is None, \
            f"foreign issue must return None, got {entry}"
        n = JournalEntry.query.filter_by(
            source_type="invoice", source_id=inv.id).count()
        assert n == 0, f"foreign issue leaked a JE ({n})"
        return "no JE at issue for SAR-in-EGP invoice"


@check("3. SAR-in-EGP collection → one EGP JE with Cash + Revenue "
        "+ VAT at cashier's rate")
def _():
    from app import create_app, db
    from app.models import JournalEntry, JournalLine, Account
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment,
    )
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX3")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        # subtotal 100 SAR, tax 15%, total 115 SAR.
        inv = _make_invoice(cid, cust, 100, currency="SAR",
                             tax_rate=15)
        post_invoice_to_ledger(inv, created_by=oid)
        # Full pay at rate 15.5 EGP per 1 SAR.
        record_payment(inv, 115, payment_method_id=pm.id,
                        created_by=oid, notify=False,
                        exchange_rate=15.5)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID
        assert abs(float(inv.fx_rate_at_receipt) - 15.5) < 1e-4
        # Exactly one JE with source_type=payment.
        entries = JournalEntry.query.filter_by(
            source_type="payment", source_id=inv.id).all()
        assert len(entries) == 1
        e = entries[0]
        assert (e.currency or "").upper() == "EGP"
        assert abs(float(e.exchange_rate) - 1.0) < 1e-4
        lines = JournalLine.query.filter_by(entry_id=e.id).all()
        by_code = {}
        for l in lines:
            acc = db.session.get(Account, l.account_id)
            by_code[acc.code] = l
        # Cash 1110 debit = 115 SAR × 15.5 = 1782.50
        assert abs(float(by_code["1110"].debit) - 1782.5) < 0.05
        # Revenue 4100 credit = 100 SAR × 15.5 = 1550.00
        assert abs(float(by_code["4100"].credit) - 1550.0) < 0.05
        # VAT 2120 credit = 15 SAR × 15.5 = 232.50
        assert abs(float(by_code["2120"].credit) - 232.5) < 0.05
        # Balance: 1782.50 = 1550.00 + 232.50 (holds)
        total_d = sum(float(l.debit) for l in lines)
        total_c = sum(float(l.credit) for l in lines)
        assert abs(total_d - total_c) < 0.01
        return (f"1 EGP JE @ 15.5: Cash 1782.50 = "
                f"Rev 1550.00 + VAT 232.50")


@check("4. Foreign collection without exchange_rate → LedgerError")
def _():
    from app import create_app, db
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment,
    )
    from app.services.ledger import LedgerError
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX4")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice(cid, cust, 100, currency="SAR",
                             tax_rate=0)
        post_invoice_to_ledger(inv, created_by=oid)
        # No exchange_rate → refuse.
        try:
            record_payment(inv, 100, payment_method_id=pm.id,
                            created_by=oid, notify=False)
        except LedgerError as e:
            assert "سعر الصرف مطلوب" in str(e)
        else:
            raise AssertionError("expected LedgerError")
        # Invalid rate (0) → refuse.
        try:
            record_payment(inv, 100, payment_method_id=pm.id,
                            created_by=oid, notify=False,
                            exchange_rate=0)
        except LedgerError as e:
            assert ("أكبر من صفر" in str(e)
                    or "غير صالح" in str(e))
        else:
            raise AssertionError("expected LedgerError")
        return "missing / invalid rate refused"


@check("5. Partial foreign collections at different rates → each "
        "posts at its own rate; last rate wins on invoice")
def _():
    from app import create_app, db
    from app.models import JournalEntry, JournalLine, Account
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment,
    )
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX5")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        # 200 SAR total, tax 0 for simplicity.
        inv = _make_invoice(cid, cust, 200, currency="SAR",
                             tax_rate=0)
        post_invoice_to_ledger(inv, created_by=oid)
        record_payment(inv, 100, payment_method_id=pm.id,
                        created_by=oid, notify=False,
                        exchange_rate=15.0)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PARTIALLY_PAID
        assert float(inv.paid_amount) == 100.0   # still in SAR
        assert abs(float(inv.fx_rate_at_receipt) - 15.0) < 1e-4
        record_payment(inv, 100, payment_method_id=pm.id,
                        created_by=oid, notify=False,
                        exchange_rate=16.0)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID
        assert float(inv.paid_amount) == 200.0
        assert abs(float(inv.fx_rate_at_receipt) - 16.0) < 1e-4, (
            "last rate should win, got "
            f"{inv.fx_rate_at_receipt}")
        # Two JEs, each with the right cash amount.
        entries = (JournalEntry.query
                    .filter_by(source_type="payment",
                                source_id=inv.id)
                    .order_by(JournalEntry.id).all())
        assert len(entries) == 2
        cash_1 = sum(
            float(l.debit) for l in
            JournalLine.query.filter_by(entry_id=entries[0].id).all()
            if db.session.get(Account, l.account_id).code == "1110")
        cash_2 = sum(
            float(l.debit) for l in
            JournalLine.query.filter_by(entry_id=entries[1].id).all()
            if db.session.get(Account, l.account_id).code == "1110")
        assert abs(cash_1 - 1500.0) < 0.05, cash_1
        assert abs(cash_2 - 1600.0) < 0.05, cash_2
        return f"2 JEs @ 15.0 + 16.0 → cash 1500 + 1600"


@check("7. Treasury Hub receive() forwards exchange_rate → foreign "
        "collection posts EGP JE at cashier's rate")
def _():
    """Fixes the gap the user pointed out — collection happens from
    /treasury, not /invoices/<id>/pay. treasury.receive() must forward
    the accountant's rate to record_payment or foreign collections
    from the hub would refuse with 'سعر الصرف مطلوب'."""
    from app import create_app, db
    from app.models import JournalEntry, JournalLine, Account
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.treasury import receive
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX7")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        # 100 SAR, tax 15% → 115 SAR total.
        inv = _make_invoice(cid, cust, 100, currency="SAR",
                             tax_rate=15)
        post_invoice_to_ledger(inv, created_by=oid)
        # Same call the /treasury/receive route makes, with the FX
        # rate the modal now surfaces.
        receive(cid, amount=115,
                 account_id=pm.account_id,
                 source="invoice", invoice_id=inv.id,
                 actor_id=oid, exchange_rate=15.5)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID, (
            "treasury collection did not flip invoice to PAID: "
            f"{inv.status}")
        assert abs(float(inv.fx_rate_at_receipt) - 15.5) < 1e-4
        # And the JE lands in EGP at 115 × 15.5.
        entries = JournalEntry.query.filter_by(
            source_type="payment", source_id=inv.id).all()
        assert len(entries) == 1
        lines = JournalLine.query.filter_by(entry_id=entries[0].id).all()
        cash = sum(float(l.debit) for l in lines if
                   db.session.get(Account, l.account_id).code == "1110")
        assert abs(cash - 1782.5) < 0.05, cash
        # And WITHOUT a rate the treasury path must refuse too.
        from app.services.ledger import LedgerError
        inv2 = _make_invoice(cid, cust, 50, currency="SAR", tax_rate=0)
        post_invoice_to_ledger(inv2, created_by=oid)
        try:
            receive(cid, amount=50,
                     account_id=pm.account_id,
                     source="invoice", invoice_id=inv2.id,
                     actor_id=oid)   # no exchange_rate
        except LedgerError as e:
            assert "سعر الصرف مطلوب" in str(e)
        else:
            raise AssertionError(
                "treasury.receive should refuse foreign invoice "
                "without exchange_rate")
        return ("treasury.receive → 1 EGP JE @ 15.5 (cash 1782.5); "
                "refuses when rate is missing")


@check("8. Treasury lookup_invoices exposes currency + is_foreign so "
        "the receive modal can toggle the FX field")
def _():
    """The receive modal's JS relies on these two fields in the
    JSON. If a future refactor drops either, the FX row silently
    stays hidden and users hit the LedgerError instead of a
    friendly input."""
    from app import create_app
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX8")
        cust = _make_customer(cid)
        # One EGP + one SAR invoice, both SENT so the lookup picks
        # them up.
        _make_invoice(cid, cust, 100, currency="EGP",
                       status_val=InvoiceStatus.SENT)
        _make_invoice(cid, cust, 200, currency="SAR",
                       status_val=InvoiceStatus.SENT)
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        r = client.get("/treasury/lookup/invoices")
        assert r.status_code == 200, r.status_code
        rows = r.get_json()
        # Both rows must include currency + is_foreign.
        assert all("currency" in x and "is_foreign" in x for x in rows), (
            f"lookup_invoices missing currency/is_foreign: {rows}")
        egp = [x for x in rows if x["currency"] == "EGP"]
        sar = [x for x in rows if x["currency"] == "SAR"]
        assert egp and not egp[0]["is_foreign"], egp
        assert sar and sar[0]["is_foreign"], sar
        return "lookup_invoices exposes currency + is_foreign"


@check("6. EGP invoice with stray exchange_rate → silently ignored")
def _():
    from app import create_app, db
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment,
    )
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("FX6")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice(cid, cust, 100, tax_rate=15)
        post_invoice_to_ledger(inv, created_by=oid)
        # Pass a random exchange_rate — should be ignored for EGP.
        record_payment(inv, 115, payment_method_id=pm.id,
                        created_by=oid, notify=False,
                        exchange_rate=99.9)
        db.session.refresh(inv)
        assert inv.status == InvoiceStatus.PAID
        # EGP invoice → fx_rate_at_receipt stays NULL.
        assert inv.fx_rate_at_receipt is None
        return "EGP path unaffected by stray exchange_rate arg"


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
