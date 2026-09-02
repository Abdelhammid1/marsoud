#!/usr/bin/env python3
"""MARSOUD-TKT-TREASURY-HUB-01 (2026-09-02) — Treasury Hub Phase 1.

The unified الخزينة surface: dashboard + قبض + دفع + تحويل + دخول
مباشر لخدمات الفواتير / فواتير الموردين / accounting_ops.

Checks:
  1. Blueprint registered with the six expected endpoints.
  2. Permission `treasury.operate` exists in P + PERMISSION_CATALOG.
  3. Feature registry: `treasury_index` under module=accounting.
  4. GET /treasury/ renders 200 + KPI + accounts card.
  5. POST /treasury/receive source=invoice delegates to record_payment
     (invoice flips PAID, no duplicate JE).
  6. POST /treasury/receive source=misc posts treasury_receipt JE.
  7. POST /treasury/pay source=vendor_bill delegates to
     record_bill_payment.
  8. Overdraft guard: pay > balance without confirm blocks; WITH
     confirm passes.
  9. POST /treasury/transfer moves money via accounting_ops
     (Σ money accounts unchanged).
 10. Company isolation: an invoice from company B cannot be
     collected via company A's treasury endpoint.
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
    # Sweep every prior TH* prefix — journal_lines have no company_id
    # so orphan lines from earlier runs can attach to freshly-inserted
    # accounts and skew the balance-based checks.
    # Sweep the current-prefix companies only. Cross-check #10 uses
    # a different prefix (TX10B) so this sweep won't wipe its
    # counterpart while it's still needed.
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
    db.session.execute(text("DELETE FROM users WHERE email LIKE '%__th%'"))
    db.session.execute(text(
        "DELETE FROM journal_entries WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN (SELECT id FROM journal_entries)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE account_id NOT IN (SELECT id FROM accounts)"))
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


def _authed_client(app, oid, cid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(oid)
        s["_fresh"] = True
        s["active_company_id"] = cid
    return c


def _prime_cash(cid, amount=1000):
    """Give the tenant's 1110 (cash) a positive opening balance so pay
    tests can exercise it. Posts a Dr 1110 / Cr 3110 (owner's capital)
    JE via post_journal — no shortcut."""
    from app import db
    from app.models import Account
    from app.services.ledger import post_journal
    cash = Account.query.filter_by(company_id=cid, code="1110").first()
    cap = Account.query.filter_by(company_id=cid, code="3110").first()
    if cap is None:
        # Fall back to 3100 header if 3110 missing
        cap = Account.query.filter_by(company_id=cid, code="3100").first()
    post_journal(company_id=cid, description="opening balance",
                 lines=[{"account_id": cash.id, "debit": amount, "credit": 0},
                        {"account_id": cap.id, "debit": 0, "credit": amount}],
                 entry_date=date.today())


def _make_invoice(cid, *, total=100):
    from datetime import timedelta
    from app import db
    from app.models import Customer, Invoice
    from app.models.invoice import InvoiceStatus
    from app.services.subsidiary import ensure_customer_account
    cust = Customer(company_id=cid, name="عميل")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(company_id=cid, number=f"INV-T-{cust.id}",
                  customer_id=cust.id, issue_date=date.today(),
                  due_date=date.today() + timedelta(days=30),
                  currency="EGP", tax_rate=Decimal("0"),
                  total=Decimal(str(total)),
                  status=InvoiceStatus.SENT)
    db.session.add(inv); db.session.commit()
    return inv


def _make_vendor_bill(cid, *, total=100):
    from datetime import timedelta
    from app import db
    from app.models import Vendor, VendorBill
    from app.models.vendor_bill import (
        VendorBillStatus, VendorBillPaymentMethod)
    from app.services.subsidiary import ensure_vendor_account
    v = Vendor(company_id=cid, name="مورد")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    b = VendorBill(company_id=cid, number=f"VB-T-{v.id}",
                    vendor_id=v.id, issue_date=date.today(),
                    due_date=date.today() + timedelta(days=30),
                    currency="EGP",
                    payment_method=VendorBillPaymentMethod.CREDIT,
                    total=Decimal(str(total)),
                    status=VendorBillStatus.POSTED)
    db.session.add(b); db.session.commit()
    return b


@check("1. blueprint registered with the six expected endpoints")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    for want in ("treasury.index", "treasury.receive_route",
                 "treasury.pay_route", "treasury.transfer_route",
                 "treasury.lookup_invoices", "treasury.lookup_vendor_bills"):
        assert want in names, f"missing endpoint: {want}"
    return "6 endpoints"


@check("2. treasury.operate in P + PERMISSION_CATALOG")
def _():
    from app.services.permissions import P
    from app.services.roles_seed import PERMISSION_CATALOG
    assert "treasury.operate" in P, "not in P"
    assert "treasury.operate" in PERMISSION_CATALOG, "not in catalog"
    grp, label, verb = PERMISSION_CATALOG["treasury.operate"]
    assert grp == "المالية والمحاسبة"
    assert "الخزينة" in label
    return f"defaults: {sorted(P['treasury.operate'])}"


@check("3. treasury_index Feature under accounting module")
def _():
    from app.services.feature_registry import all_features, all_modules
    feats = {f.code: f for f in all_features()}
    assert "treasury_index" in feats, "feature not registered"
    f = feats["treasury_index"]
    assert f.module == "accounting"
    mods = {m.code for m in all_modules()}
    assert "accounting" in mods
    return "wired to accounting module"


@check("4. GET /treasury/ renders 200 with KPI + account cards")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH4")
        try:
            _prime_cash(cid, amount=500)
            r = _authed_client(app, oid, cid).get("/treasury/")
            assert r.status_code == 200, (
                f"got {r.status_code} → {r.headers.get('Location')}")
            html = r.data.decode("utf-8")
            assert "الخزينة" in html and "المجموع الكلي" in html
            assert "الصندوق" in html
            return "dashboard renders with KPI + accounts"
        finally:
            pass  # teardown happens on next _boot


@check("5. receive source=invoice flips invoice to PAID + one JE only")
def _():
    from app import create_app, db
    from app.models import Invoice
    from app.models.invoice import InvoiceStatus
    from app.models.journal import JournalEntry
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH5")
        try:
            inv = _make_invoice(cid, total=250)
            before = JournalEntry.query.filter_by(company_id=cid).count()
            cash_id = None
            from app.models import Account
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            cash_id = cash.id
            r = _authed_client(app, oid, cid).post("/treasury/receive", data={
                "amount": "250",
                "account_id": cash_id,
                "source": "invoice",
                "invoice_id": inv.id,
            })
            assert r.status_code in (302, 303)
            db.session.refresh(inv)
            assert inv.status == InvoiceStatus.PAID, \
                f"expected PAID, got {inv.status}"
            after = JournalEntry.query.filter_by(company_id=cid).count()
            # Exactly ONE new JE — the payment JE from record_payment.
            assert after - before == 1, \
                f"expected +1 JE, got +{after - before}"
            return f"invoice PAID + 1 JE (as expected)"
        finally:
            pass


@check("6. receive source=misc posts treasury_receipt JE")
def _():
    from app import create_app, db
    from app.models import Account
    from app.models.journal import JournalEntry
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH6")
        try:
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            r = _authed_client(app, oid, cid).post("/treasury/receive", data={
                "amount": "125",
                "account_id": cash.id,
                "source": "misc",
                "note": "قبض عام تجريبي",
            })
            assert r.status_code in (302, 303)
            je = (JournalEntry.query
                  .filter_by(company_id=cid,
                              source_type="treasury_receipt")
                  .first())
            assert je is not None, "no treasury_receipt JE"
            assert je.description == "قبض عام تجريبي"
            db.session.refresh(cash)
            assert float(cash.balance or 0) == 125.0, \
                f"cash should be 125, got {cash.balance}"
            return "misc receipt posted + cash increased"
        finally:
            pass


@check("7. pay source=vendor_bill flips bill to PAID")
def _():
    from app import create_app, db
    from app.models import Account, VendorBill
    from app.models.vendor_bill import VendorBillStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH7")
        try:
            _prime_cash(cid, amount=1000)
            bill = _make_vendor_bill(cid, total=400)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            r = _authed_client(app, oid, cid).post("/treasury/pay", data={
                "amount": "400",
                "account_id": cash.id,
                "source": "vendor_bill",
                "vendor_bill_id": bill.id,
                "confirm_overdraft": "1",  # cover any dust
            })
            assert r.status_code in (302, 303)
            db.session.refresh(bill)
            assert bill.status == VendorBillStatus.PAID, \
                f"expected PAID, got {bill.status}"
            return "bill flipped to PAID"
        finally:
            pass


@check("8. overdraft guard — refuse without confirm, allow with")
def _():
    from app import create_app, db
    from app.models import Account
    from app.models.journal import JournalEntry
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH8")
        try:
            _prime_cash(cid, amount=100)  # only 100 available
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            client = _authed_client(app, oid, cid)
            before = JournalEntry.query.filter_by(
                company_id=cid, source_type="treasury_payment").count()
            # Attempt 1: NO confirm — must be blocked.
            r = client.post("/treasury/pay", data={
                "amount": "500",
                "account_id": cash.id,
                "source": "misc",
                "note": "over-draft attempt",
            })
            assert r.status_code in (302, 303)
            mid = JournalEntry.query.filter_by(
                company_id=cid, source_type="treasury_payment").count()
            assert mid == before, \
                f"overdraft was allowed without confirm — before={before} mid={mid}"
            # Attempt 2: WITH confirm — must go through.
            r = client.post("/treasury/pay", data={
                "amount": "500",
                "account_id": cash.id,
                "source": "misc",
                "note": "over-draft confirmed",
                "confirm_overdraft": "1",
            })
            assert r.status_code in (302, 303)
            after = JournalEntry.query.filter_by(
                company_id=cid, source_type="treasury_payment").count()
            assert after == before + 1, \
                f"confirmed overdraft did not post — before={before} after={after}"
            return "block without confirm, allow with"
        finally:
            pass


@check("9. transfer moves money without changing Σ money accounts")
def _():
    from app import create_app, db
    from app.models import Account
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TH9")
        try:
            _prime_cash(cid, amount=1000)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            # Pick any bank leaf. If none, fall back to 1120 tree
            bank = (Account.query.filter_by(company_id=cid, code="1121").first()
                    or Account.query.filter_by(company_id=cid, code="1124").first()
                    or Account.query.filter_by(company_id=cid, code="1122").first())
            assert bank is not None, "no bank leaf in seed COA"
            before = float(cash.balance or 0) + float(bank.balance or 0)
            r = _authed_client(app, oid, cid).post("/treasury/transfer", data={
                "from_id": cash.id,
                "to_id": bank.id,
                "amount": "300",
            })
            assert r.status_code in (302, 303)
            db.session.refresh(cash); db.session.refresh(bank)
            after = float(cash.balance or 0) + float(bank.balance or 0)
            assert abs(after - before) < 0.01, \
                f"total moved from {before} to {after} (should be equal)"
            assert float(cash.balance or 0) == 700.0
            assert float(bank.balance or 0) == 300.0
            return f"cash {cash.balance} / bank {bank.balance} (Σ preserved)"
        finally:
            pass


@check("10. company isolation — B's invoice cannot be paid via A")
def _():
    from app import create_app, db
    from app.models import Invoice
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        # Company A + Company B — use DIFFERENT prefixes so the
        # per-prefix sweep in _boot doesn't wipe A when B boots.
        email_a, cid_a, oid_a = _boot("TH10A")
        try:
            # B uses a non-TH prefix so the __TH sweep doesn't kill A.
            email_b, cid_b, oid_b = _boot("TX10B")
            inv_b = _make_invoice(cid_b, total=100)
            # A's user + A's cash
            from app.models import Account
            cash_a = Account.query.filter_by(company_id=cid_a, code="1110").first()
            # Log in as A's owner, active_company=A, POST with B's invoice
            r = _authed_client(app, oid_a, cid_a).post("/treasury/receive", data={
                "amount": "100",
                "account_id": cash_a.id,
                "source": "invoice",
                "invoice_id": inv_b.id,
            })
            assert r.status_code in (302, 303)
            db.session.refresh(inv_b)
            # Invoice B must NOT have been flipped — cross-tenant post refused.
            assert inv_b.status == InvoiceStatus.SENT, \
                f"cross-tenant leak: B's invoice flipped to {inv_b.status}"
            return "cross-tenant post refused (invoice B stays SENT)"
        finally:
            pass


def main():
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
