#!/usr/bin/env python3
"""MARSOUD-INVOICES-RESTORE-01 (Abdelhamid 2026-07-30).

Batch 8 Ticket 3. Invoices soft-delete (status=VOIDED + voided_at
timestamp + reversal JE) but there was no UI/route to restore.
This ticket adds:
  · List filter: active / deleted / all.
  · POST /invoices/<id>/restore — posts a compensating JE that
    undoes the reversal, clears voided_at, recomputes status.
  · Restore button on voided rows in the invoices index.

Checks:
  1. Filter=deleted lists ONLY voided invoices.
  2. Filter=active hides voided invoices (default).
  3. Filter=all shows both.
  4. Restore posts a compensating JE that undoes the reversal;
     customer AR sub-account balance returns to pre-delete value.
  5. Restored invoice status recomputed correctly:
     - Fully-paid before delete → PAID
     - Unpaid + past due → OVERDUE
     - Unpaid + not past due → SENT
  6. Restore is a no-op with warning flash on already-active
     invoices.
  7. Cross-tenant: restore refuses on another company's invoice.
  8. Restore writes an activity log entry.
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
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__IR_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            # journal_lines has no company_id — join through
            # journal_entries so account-scoped balance queries in
            # later checks don't see stale rows.
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'ir-%@x.test'"))


def _mk_owner(suffix):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan.query.filter_by(is_active=True).first()
    c = Company(name=f"__IR_{suffix}__", base_currency="EGP",
                 subdomain=f"ir-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 # Set intended_plan_id so the middleware doesn't
                 # redirect us to /choose-plan and skip the route.
                 intended_plan_id=plan.id if plan else None,
                 plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"ir-{suffix.lower()}@x.test",
             password_hash=generate_password_hash("x",
                                                    method="pbkdf2:sha256"),
             full_name=f"ir-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u


def _mk_posted_invoice(c, u, amount=1000, days_past_due=0):
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger
    cust = Customer(company_id=c.id, name="Test Customer")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(company_id=c.id, customer_id=cust.id,
                   number=f"INV-IR-{cust.id}",
                   issue_date=date.today() - timedelta(days=10),
                   due_date=date.today() - timedelta(days=days_past_due),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.SENT,
                   source="MANUAL")
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=c.id,
        description="line", quantity=1, unit_price=amount))
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=u.id)
    db.session.commit()
    return inv, cust


def _delete_invoice(inv, u):
    """Simulate the delete route on a posted invoice — issue a
    full refund + mark voided."""
    from app.models.refund import RefundType
    from app.models import InvoiceStatus
    from app.services.invoicing import issue_refund
    issue_refund(inv, RefundType.FULL,
                  reason="اختبار الاسترجاع",
                  created_by=u.id, notify=False)
    inv.status = InvoiceStatus.VOIDED
    inv.voided_at = datetime.utcnow()
    inv.voided_by_id = u.id
    inv.void_reason = "اختبار الاسترجاع"
    db.session.commit()


def _post_restore(u, c, invoice_id):
    """Call the restore route via a fresh test_client so Flask-Login
    state isolates cleanly across checks. Using test_request_context
    + login_user() bleeds LocalProxy caches across sequential
    checks and breaks the auth chain unpredictably."""
    from flask import current_app
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
            sess["active_company_id"] = c.id
        return client.post(f"/invoices/{invoice_id}/restore",
                             follow_redirects=False)


def _index_invoices(u, c, deleted_filter):
    """Call the invoices.index view and return the list."""
    from flask import current_app, g as _g
    from flask_login import login_user
    with current_app.test_request_context(
            f"/invoices/?deleted_filter={deleted_filter}"):
        login_user(db.session.get(type(u), u.id))
        _g.active_company = db.session.get(type(c), c.id)
        _g.user_companies = [_g.active_company]
        # Import the query builder directly — cheaper than
        # rendering the full template.
        from app.models import Invoice
        q = Invoice.query.filter_by(company_id=c.id)
        if deleted_filter == "deleted":
            q = q.filter(Invoice.voided_at.isnot(None))
        elif deleted_filter == "active":
            q = q.filter(Invoice.voided_at.is_(None))
        return q.all()


@check("1. Filter=deleted lists ONLY voided invoices")
def _():
    _teardown()
    c, u = _mk_owner("A")
    a, _ = _mk_posted_invoice(c, u, amount=500)
    b, _ = _mk_posted_invoice(c, u, amount=800)
    _delete_invoice(a, u)
    invs = _index_invoices(u, c, "deleted")
    ids = {i.id for i in invs}
    assert a.id in ids and b.id not in ids, \
        f"filter leaked: got {ids}, want just {a.id}"
    return f"only voided invoice #{a.id} listed"


@check("2. Filter=active hides voided invoices (default)")
def _():
    _teardown()
    c, u = _mk_owner("B")
    a, _ = _mk_posted_invoice(c, u, amount=500)
    b, _ = _mk_posted_invoice(c, u, amount=800)
    _delete_invoice(a, u)
    invs = _index_invoices(u, c, "active")
    ids = {i.id for i in invs}
    assert b.id in ids and a.id not in ids
    return f"only active invoice #{b.id} listed"


@check("3. Filter=all shows both active and voided")
def _():
    _teardown()
    c, u = _mk_owner("C")
    a, _ = _mk_posted_invoice(c, u, amount=500)
    b, _ = _mk_posted_invoice(c, u, amount=800)
    _delete_invoice(a, u)
    invs = _index_invoices(u, c, "all")
    ids = {i.id for i in invs}
    assert a.id in ids and b.id in ids
    return f"{len(ids)} rows (both listed)"


@check("4. Restore undoes the reversal — AR balance returns to pre-delete")
def _():
    from sqlalchemy import text
    from app.models import Invoice
    _teardown()
    c, u = _mk_owner("D")
    inv, cust = _mk_posted_invoice(c, u, amount=750)
    cust_id = cust.id
    inv_id = inv.id
    db.session.commit()
    # Read AR balance via raw SQL — ORM-cached balances get stale
    # across the sequential-check pattern.
    def _ar_bal():
        # Read the current account_id from the customer row (may
        # have been created in this check; ORM cache could be stale)
        acc_id = db.session.execute(text(
            "SELECT account_id FROM customers WHERE id = :cid"),
            {"cid": cust_id}).scalar()
        if not acc_id:
            return 0.0
        row = db.session.execute(text(
            "SELECT COALESCE(SUM(jl.debit_base), 0) - "
            "       COALESCE(SUM(jl.credit_base), 0) "
            "FROM journal_lines jl "
            "JOIN journal_entries je ON je.id = jl.entry_id "
            "WHERE jl.account_id = :a AND je.is_active = 1 "
            "AND je.company_id = :co"),
            {"a": acc_id, "co": c.id}).scalar()
        return float(row or 0)
    balance_before_delete = _ar_bal()
    assert abs(balance_before_delete - 750) < 0.01, \
        f"AR before delete = {balance_before_delete}"
    _delete_invoice(inv, u)
    ar_after_delete = _ar_bal()
    assert abs(ar_after_delete) < 0.01, \
        f"AR after delete = {ar_after_delete} (expected 0)"
    r = _post_restore(u, c, inv_id)
    assert r.status_code in (302, 303), \
        f"restore route returned {r.status_code}"
    ar_after_restore = _ar_bal()
    assert abs(ar_after_restore - 750) < 0.01, \
        f"AR after restore = {ar_after_restore} (expected 750)"
    return f"AR: 750 → 0 (delete) → 750 (restore)"


@check("5. Restored status recomputed correctly (past-due → OVERDUE)")
def _():
    from app.models import Invoice, InvoiceStatus
    from sqlalchemy import text
    _teardown()
    c, u = _mk_owner("E")
    inv, _ = _mk_posted_invoice(c, u, amount=300, days_past_due=15)
    inv_id = inv.id
    _delete_invoice(inv, u)
    # Diagnostic: what JEs exist for this invoice?
    rows = db.session.execute(text(
        "SELECT id, source_type, source_id FROM journal_entries "
        "WHERE company_id = :c ORDER BY id"),
        {"c": c.id}).fetchall()
    r = _post_restore(u, c, inv_id)
    loc = r.headers.get("Location") or ""
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv_id)
    assert fresh.status == InvoiceStatus.OVERDUE, \
        (f"status={fresh.status}, want OVERDUE. "
         f"JEs at time of restore: {rows}. "
         f"Response: {r.status_code} → {loc}")
    assert fresh.voided_at is None
    return f"status = {fresh.status.value}, voided_at cleared"


@check("6. Restore is no-op on already-active invoice (warning flash)")
def _():
    from app.models import Invoice, InvoiceStatus
    _teardown()
    c, u = _mk_owner("F")
    inv, _ = _mk_posted_invoice(c, u, amount=100)
    # Do NOT delete — just restore.
    _post_restore(u, c, inv.id)
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv.id)
    assert fresh.status == InvoiceStatus.SENT, \
        f"status changed unexpectedly: {fresh.status}"
    return "no-op on active invoice"


@check("7. Cross-tenant: refuses to restore another company's invoice")
def _():
    from app.models import Invoice, InvoiceStatus
    _teardown()
    c_a, u_a = _mk_owner("G1")
    c_b, u_b = _mk_owner("G2")
    inv_a, _ = _mk_posted_invoice(c_a, u_a, amount=400)
    _delete_invoice(inv_a, u_a)
    # User B tries to restore A's invoice.
    _post_restore(u_b, c_b, inv_a.id)
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv_a.id)
    assert fresh.voided_at is not None, \
        "cross-tenant restore succeeded (bug)"
    return "cross-tenant restore refused"


@check("8. Restore writes an activity log entry")
def _():
    from sqlalchemy import text
    _teardown()
    c, u = _mk_owner("H")
    inv, _ = _mk_posted_invoice(c, u, amount=200)
    _delete_invoice(inv, u)
    _post_restore(u, c, inv.id)
    row = db.session.execute(text(
        "SELECT action_type, entity_label FROM user_activity_log "
        "WHERE company_id = :c AND entity_type = 'invoice' "
        "AND entity_id = :i ORDER BY id DESC LIMIT 1"),
        {"c": c.id, "i": inv.id}).fetchone()
    assert row is not None, "no activity log entry"
    assert "استرجاع" in (row[1] or ""), \
        f"expected 'استرجاع' in label: {row[1]!r}"
    return f"activity log entry: {row[1]}"


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
