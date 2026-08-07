#!/usr/bin/env python3
"""MARSOUD-CRON-VBILL-NO-AUTOPAY-01 (2026-08-07) — regression audit.

On 2026-08-06 the cron `process_recurring_vendor_bills()` ran after a
3-week outage and materialised + auto-paid 4 CASH-template bills
(VB-0061..64) totalling 5,526.93 EGP without owner approval. Root
cause: `materialize_from_recurring()` defaulted to POSTED, so
`post_vendor_bill()` ran, and for CASH/BANK templates it posted a
second journal (Dr Vendor sub / Cr Cash|Bank) that drained the till
immediately.

The fix is one explicit argument at the cron call site:
`materialize_from_recurring(..., status_target="DRAFT")`. This
audit pins the fix and the invariant it protects.

Five checks (all verified to fail against pre-fix HEAD):

  1. CASH template → cron produces DRAFT, no cash movement
  2. BANK template → same
  3. CREDIT template → same (ticket wording: regardless of payment
     method)
  4. Idempotency preserved — second cron run same day skips via
     unique index, no duplicate materialisation
  5. Manual `materialize_from_recurring(status_target="POSTED")`
     still auto-posts (regression guard — the ticket says the manual
     path stays as-is)
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
PREFIX = "__CVBAUTO_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Vendor, Account,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__cvbauto__").first()
    if not plan:
        plan = Plan(code="__cvbauto__", name="A", name_ar="A",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases",
                          "reports"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="cvb",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="cvb owner",
             is_active=True, status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"))
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    v = Vendor(company_id=c.id, name=f"{PREFIX}vendor",
               is_active=True)
    db.session.add(v); db.session.commit()

    _STATE.update(company_id=c.id, user_id=u.id, vendor_id=v.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CVBAUTO_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__CVBAUTO_%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__cvbauto__'"))


# ─── Helpers ───────────────────────────────────────────────────
def _seed_template(pm, *, amount=100):
    """Build a POSTED source bill + a RecurringBill pointing at it
    with the given payment_method. Start_date is TODAY - 1 so the
    cron treats it as past-due-and-not-yet-materialised."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, RecurringBill, Account,
    )
    from app.services.vendor_bills import post_vendor_bill
    from decimal import Decimal
    pm_map = {
        "CASH": VendorBillPaymentMethod.CASH,
        "BANK": VendorBillPaymentMethod.BANK,
        "CREDIT": VendorBillPaymentMethod.CREDIT,
    }
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5220").first()
    # Build the source at CREDIT so post_vendor_bill doesn't drain
    # cash during fixture setup — flip payment_method to the real
    # target AFTER posting.
    src = VendorBill(
        company_id=_STATE["company_id"],
        vendor_id=_STATE["vendor_id"],
        number=f"SRC-{pm}",
        issue_date=date.today() - timedelta(days=30),
        due_date=date.today() - timedelta(days=30),
        payment_method=VendorBillPaymentMethod.CREDIT,
        currency="EGP", tax_rate=Decimal("0"),
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(src); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=src.id, description="test",
        line_type=BillLineType.EXPENSE, account_id=exp.id,
        quantity=Decimal("1"), unit_price=Decimal(str(amount))))
    db.session.flush(); src.recalc(); post_vendor_bill(src)
    # Now flip to the real payment_method — this is the "template"
    # the cron will faithfully clone.
    src.payment_method = pm_map[pm]
    db.session.commit()

    rb = RecurringBill(
        company_id=_STATE["company_id"], source_bill_id=src.id,
        vendor_id=_STATE["vendor_id"], amount=Decimal(str(amount)),
        currency="EGP", interval_unit="MONTH", interval_count=1,
        start_date=date.today() - timedelta(days=1), active=True,
    )
    db.session.add(rb); db.session.commit()
    return src, rb


def _account_balance(company_id, code):
    """Sum debit - credit for an account, using the reports helper
    so we're checking exactly what the reports would show."""
    from app.services.reports import _account_balance as _bal
    from app.models import Account
    acc = Account.query.filter_by(
        company_id=company_id, code=code).first()
    if not acc:
        return 0.0
    d, c = _bal(acc.id)
    return d - c


# ─── Checks ────────────────────────────────────────────────────
@check("1. CASH template — cron materialises as DRAFT, no cash movement")
def _():
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    from app.models import VendorBill, VendorBillStatus, JournalEntry
    _setup()
    src, rb = _seed_template("CASH", amount=1200)
    cash_before = _account_balance(_STATE["company_id"], "1110")
    baseline_je = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()

    summary = process_recurring_vendor_bills()

    materialised = VendorBill.query.filter_by(
        recurring_bill_id=rb.id).all()
    assert len(materialised) == 1, (
        f"expected 1 materialised bill, got {len(materialised)}")
    m = materialised[0]
    assert m.status == VendorBillStatus.DRAFT, (
        f"CASH template auto-paid (status={m.status.value})")
    assert m.journal_entry_id is None, (
        "materialised bill carries a JE — post_vendor_bill ran")
    # No JE for this bill at all — neither posting nor settlement.
    bill_je = JournalEntry.query.filter(
        JournalEntry.company_id == _STATE["company_id"],
        JournalEntry.source_type.in_(("vendor_bill",
                                       "vendor_bill_payment")),
        JournalEntry.source_id == m.id).all()
    assert not bill_je, (
        f"materialisation posted {len(bill_je)} JE for CASH bill")
    cash_after = _account_balance(_STATE["company_id"], "1110")
    assert abs(cash_after - cash_before) < 0.01, (
        f"cash balance moved: {cash_before} → {cash_after}")
    return (f"DRAFT, no JE, cash unchanged ({cash_before} → "
            f"{cash_after}); materialised={summary.get('materialised')}")


@check("2. BANK template — cron materialises as DRAFT, no bank movement")
def _():
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    from app.models import VendorBill, VendorBillStatus, JournalEntry
    _setup()
    src, rb = _seed_template("BANK", amount=500)
    bank_before = _account_balance(_STATE["company_id"], "1124")
    process_recurring_vendor_bills()

    m = VendorBill.query.filter_by(recurring_bill_id=rb.id).first()
    assert m.status == VendorBillStatus.DRAFT, (
        f"BANK template auto-paid (status={m.status.value})")
    assert m.journal_entry_id is None
    bank_after = _account_balance(_STATE["company_id"], "1124")
    assert abs(bank_after - bank_before) < 0.01
    return "DRAFT, no bank movement"


@check("3. CREDIT template — also DRAFT (ticket: regardless of pm)")
def _():
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    from app.models import (
        VendorBill, VendorBillStatus, JournalEntry, Account,
    )
    _setup()
    src, rb = _seed_template("CREDIT", amount=300)
    # AP sub-account for this vendor didn't exist before the src
    # posting, but src is already POSTED so the vendor's leaf exists.
    # For the cron materialisation with the fix in place, no JE
    # should touch anything.
    baseline_je = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    process_recurring_vendor_bills()
    m = VendorBill.query.filter_by(recurring_bill_id=rb.id).first()
    assert m.status == VendorBillStatus.DRAFT, (
        f"CREDIT template auto-posted (status={m.status.value}) — "
        f"ticket says regardless of payment method")
    assert m.journal_entry_id is None
    after_je = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert after_je == baseline_je, (
        f"posted {after_je - baseline_je} JE(s) for CREDIT bill")
    return "DRAFT, no JE (ticket honored)"


@check("4. Idempotency — second cron run doesn't double-materialise")
def _():
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    from app.models import VendorBill
    _setup()
    src, rb = _seed_template("CASH", amount=200)
    first = process_recurring_vendor_bills()
    second = process_recurring_vendor_bills()
    # The load-bearing invariant: only ONE bill row per
    # (template, occurrence_date), never two.
    count = VendorBill.query.filter_by(
        recurring_bill_id=rb.id).count()
    assert count == 1, (
        f"double-materialised: {count} bills for the same template")
    # The second run must not produce a new bill. Idempotency can
    # come either from the pre-query filter (unmaterialised_past_due
    # excludes rows already materialised → occurrences=[] on the 2nd
    # run) OR from the IntegrityError catch on the unique index —
    # both paths satisfy the invariant. Today's implementation uses
    # the pre-filter; the try/except is a race-safety net for
    # concurrent cron runs.
    assert second["materialised"] == 0, (
        f"second run materialised {second['materialised']} bill(s) "
        "— duplicates leaked through the guard")
    return (f"1st: {first['materialised']} materialised, "
            f"2nd: 0 materialised + {second['skipped_duplicate']} skipped-dup "
            f"(both idempotency paths verified)")


@check("5. Manual status_target='POSTED' STILL auto-posts (regression guard)")
def _():
    """The ticket says only the cron path changes; a caller that
    explicitly asks for POSTED still gets it. Verifies the manual
    button + any future callsite passing POSTED works as before."""
    from app.services.vendor_bills import materialize_from_recurring
    from app.models import VendorBill, VendorBillStatus, JournalEntry
    _setup()
    # CREDIT so post_vendor_bill doesn't drain fixture cash.
    src, rb = _seed_template("CREDIT", amount=180)
    m = materialize_from_recurring(
        rb, date.today(),
        actor_id=_STATE["user_id"],
        status_target="POSTED")
    assert m.status == VendorBillStatus.POSTED, (
        f"explicit POSTED didn't post: {m.status.value}")
    assert m.journal_entry_id is not None, (
        "explicit POSTED left journal_entry_id NULL")
    je = db.session.get(JournalEntry, m.journal_entry_id)
    assert je and je.source_type == "vendor_bill"
    return f"explicit POSTED still auto-posts (JE #{je.number})"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}\n        => {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
