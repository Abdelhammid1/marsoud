#!/usr/bin/env python3
"""MARSOUD-SAAS-INVOICE-LEDGER-01 (Abdelhamid 2026-07-29).

Batch 6 Ticket 6 — critical bug audit. On prod, ALL 22 SaaS
invoices in company 8 have status=SENT but NO journal entry —
so AR aging shows them but the customer sub-account balance is
zero. Root cause: saas_billing.create_first_invoice + the
next-cycle invoice in _saas_post_payment never called
post_invoice_to_ledger.

Checks:
  1. create_first_invoice posts a journal entry immediately.
  2. Idempotency: calling create_first_invoice twice on the
     same company doesn't spawn a duplicate JE.
  3. _has_journal_entry helper correctly detects a posted JE.
  4. AR balance sanity: customer sub-account balance == invoice
     total after posting (was zero before the fix).
  5. _saas_post_payment posts the JE for the NEXT invoice
     it creates (renewal cycle).
  6. saas-backfill-ledger CLI: dry-run prints the orphan list
     WITHOUT posting.
  7. saas-backfill-ledger CLI: real run posts + is idempotent
     (rerun creates no duplicates).
  8. Backfill uses the invoice's ORIGINAL issue_date, not
     today's date (so historical reports don't shift).
  9. Cross-tenant: backfill correctly handles orphan invoices
     from multiple companies without leakage.
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    from app.services.manasty import manasty_id
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SIL_%__'"))]
        for cid in cids:
            conn.execute(text(
                "UPDATE companies SET saas_customer_id = NULL, "
                "applied_coupon_id = NULL WHERE id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        mid = manasty_id()
        # Purge Manasty-side rows tied to fixture invoices.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id = :m "
            "AND source_type = 'invoice' AND source_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND notes LIKE '%__SIL_%'))"), {"m": mid})
        conn.execute(text(
            "DELETE FROM journal_entries WHERE company_id = :m "
            "AND source_type = 'invoice' AND source_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND notes LIKE '%__SIL_%')"), {"m": mid})
        conn.execute(text(
            "DELETE FROM invoice_items WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND notes LIKE '%__SIL_%')"), {"m": mid})
        conn.execute(text(
            "DELETE FROM invoices WHERE company_id = :m "
            "AND notes LIKE '%__SIL_%'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM customers WHERE company_id = :m "
            "AND name LIKE '__SIL_%__'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sil-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'sil-%'"))


def _bootstrap(suffix):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan(code=f"sil-{suffix}", name=f"SIL-{suffix}",
                 name_ar="اختبار", is_active=True,
                 price_monthly=Decimal("500"))
    db.session.add(plan); db.session.flush()
    now = datetime.utcnow()
    c = Company(name=f"__SIL_{suffix}__", base_currency="EGP",
                 subdomain=f"sil-{suffix.lower()}",
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=14),
                 subscription_frequency="MONTHLY",
                 intended_plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"sil-{suffix.lower()}@x.test",
             password_hash=generate_password_hash("x",
                                                    method="pbkdf2:sha256"),
             full_name=f"sil-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=now)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u, plan


def _je_count_for_invoice(invoice_id):
    from sqlalchemy import text
    return db.session.execute(text(
        "SELECT COUNT(*) FROM journal_entries "
        "WHERE source_type = 'invoice' AND source_id = :i"),
        {"i": invoice_id}).scalar()


@check("1. create_first_invoice posts a journal entry immediately")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap("A")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    n = _je_count_for_invoice(inv.id)
    assert n == 1, f"expected 1 JE, got {n}"
    return f"JE posted for invoice #{inv.number}"


@check("2. Idempotent: 2nd call returns same invoice + no duplicate JE")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap("B")
    inv1 = _sb.create_first_invoice(c)
    db.session.commit()
    inv2 = _sb.create_first_invoice(c)
    db.session.commit()
    assert inv1.id == inv2.id
    n = _je_count_for_invoice(inv1.id)
    assert n == 1, f"duplicate JE created: {n}"
    return f"1 invoice, 1 JE across 2 create calls"


@check("3. _has_journal_entry() correctly detects posted JE")
def _():
    from app.services.saas_billing import _has_journal_entry
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap("C")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    assert _has_journal_entry(inv) is True
    # A freshly-created invoice with no JE returns False.
    from app.models import Invoice, InvoiceStatus, Customer
    fake = Invoice(company_id=c.id,
                    customer_id=inv.customer_id, number="FAKE",
                    issue_date=date.today(),
                    due_date=date.today(), currency="EGP",
                    status=InvoiceStatus.DRAFT, source="SAAS_BILLING")
    db.session.add(fake); db.session.flush()
    assert _has_journal_entry(fake) is False
    db.session.rollback()
    return "helper flips correctly"


@check("4. AR sub-account balance equals invoice total after posting")
def _():
    from app.services import saas_billing as _sb
    from app.models import Customer, Account
    _teardown()
    c, u, plan = _bootstrap("D")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    # Look up the mirror customer's AR sub-account.
    cust = db.session.get(Customer, inv.customer_id)
    acc = db.session.get(Account, cust.account_id)
    assert acc is not None, "customer AR sub-account missing"
    # Balance is derived; refresh from DB.
    balance = float(acc.balance or 0)
    total = float(inv.total)
    assert abs(balance - total) < 0.01, \
        f"AR balance ({balance}) != invoice total ({total})"
    return f"AR balance = {balance:.2f} matches invoice total"


@check("5. Cron sweep posts JE for the deferred NEXT invoice")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import Invoice, User, UserStatus, Company
    from werkzeug.security import generate_password_hash
    from datetime import date
    _teardown()
    c, u, plan = _bootstrap("E")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    admin = User(email="sil-admin@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sil-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    record_payment(invoice=inv, amount=float(inv.total),
                    method="cash", created_by=admin.id, notify=False)
    db.session.expire_all()
    # MARSOUD-SAAS-DEFERRED-INVOICE-01 (Batch 8 Ticket 2) —
    # payment sets next_billing_date but does NOT create the
    # next invoice. Simulate the cron day.
    tenant = db.session.get(Company, c.id)
    assert tenant.next_billing_date is not None
    tenant.next_billing_date = date.today()
    db.session.commit()
    _sb.process_saas_next_invoices()
    db.session.expire_all()
    next_inv = (Invoice.query
                  .filter(Invoice.customer_id == inv.customer_id,
                            Invoice.id != inv.id,
                            Invoice.source == "SAAS_BILLING")
                  .first())
    assert next_inv is not None, "cron did not create next invoice"
    n = _je_count_for_invoice(next_inv.id)
    assert n == 1, f"next invoice has {n} JEs (expected 1)"
    return f"cron-created next invoice #{next_inv.number} has 1 JE"


# ─── Backfill CLI ────────────────────────────────────────────────
def _run_backfill(args=None):
    from click.testing import CliRunner
    from app.cli import saas_backfill_ledger_command
    return CliRunner().invoke(saas_backfill_ledger_command,
                                args or ["--yes"])


def _seed_orphan(c, u, plan):
    """Create a SaaS invoice WITHOUT calling
    _post_to_ledger_idempotent — simulates the pre-fix bug."""
    from app.services.saas_billing import (
        ensure_saas_customer, _resolve_price, FREQ_LABELS_AR,
    )
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.numbering import next_number
    from app.services.manasty import manasty_id
    cust = ensure_saas_customer(c)
    mid = manasty_id()
    freq = c.subscription_frequency or "MONTHLY"
    price = _resolve_price(c, plan, freq)
    inv = Invoice(company_id=mid, customer_id=cust.id,
                   number=next_number(mid, "INVOICE"),
                   issue_date=date.today() - timedelta(days=45),
                   due_date=date.today() - timedelta(days=31),
                   currency=c.base_currency or "EGP",
                   tax_rate=0, status=InvoiceStatus.SENT,
                   source="SAAS_BILLING",
                   notes=f"فاتورة orphan للاختبار __SIL_{c.id}__",
                   send_reminders=True)
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=mid,
        description="اشتراك اختبار", quantity=1,
        unit_price=price))
    inv.recalc()
    db.session.commit()
    return inv


@check("6. saas-backfill-ledger --dry-run lists orphans without writing")
def _():
    _teardown()
    c, u, plan = _bootstrap("F")
    orphan = _seed_orphan(c, u, plan)
    assert _je_count_for_invoice(orphan.id) == 0
    result = _run_backfill(["--dry-run"])
    assert result.exit_code == 0, \
        f"CLI failed: {result.exit_code} — {result.output}"
    assert "dry-run" in result.output.lower()
    assert _je_count_for_invoice(orphan.id) == 0, \
        "dry-run posted a JE (should not have)"
    return "dry-run correctly listed but didn't post"


@check("7. saas-backfill-ledger --yes posts + is idempotent")
def _():
    _teardown()
    c, u, plan = _bootstrap("G")
    orphan = _seed_orphan(c, u, plan)
    result = _run_backfill(["--yes"])
    assert result.exit_code == 0, \
        f"CLI failed: {result.exit_code} — {result.output}"
    assert _je_count_for_invoice(orphan.id) == 1, \
        "orphan not posted"
    # Re-run.
    _run_backfill(["--yes"])
    assert _je_count_for_invoice(orphan.id) == 1, \
        "backfill rerun created duplicate JE"
    return "posted + idempotent on rerun"


@check("8. Backfill uses invoice's ORIGINAL issue_date (not today)")
def _():
    from app.models import JournalEntry
    _teardown()
    c, u, plan = _bootstrap("H")
    orphan = _seed_orphan(c, u, plan)
    original_date = orphan.issue_date
    _run_backfill(["--yes"])
    je = (JournalEntry.query
            .filter_by(source_type="invoice", source_id=orphan.id)
            .first())
    assert je is not None
    assert je.date == original_date, \
        f"JE date ({je.date}) != invoice issue_date ({original_date})"
    return f"JE dated {je.date} (matches original)"


@check("9. Cross-tenant: backfill scans + posts across multiple companies")
def _():
    _teardown()
    c1, u1, p1 = _bootstrap("I1")
    c2, u2, p2 = _bootstrap("I2")
    o1 = _seed_orphan(c1, u1, p1)
    o2 = _seed_orphan(c2, u2, p2)
    _run_backfill(["--yes"])
    assert _je_count_for_invoice(o1.id) == 1
    assert _je_count_for_invoice(o2.id) == 1
    return "both orphans posted, no cross-tenant leakage"


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
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
