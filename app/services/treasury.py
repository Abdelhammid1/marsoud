"""MARSOUD-TKT-TREASURY-HUB-01 (2026-09-02) — Treasury Hub service.

A thin orchestration layer that surfaces cash / bank operations from a
single screen without reinventing accounting flows:

  * `list_balances`  — every cash/bank account for the tenant with live
                       balance, grouped الصندوق / البنوك.
  * `kpi`            — dashboard summary: total_cash + total_bank +
                       combined + account_count.
  * `receive`        — قبض. On an invoice → delegates to
                       `invoicing.record_payment` so the invoice status,
                       Payment row and commission-firing behave identically
                       to `/invoices/<id>/pay`. On misc → posts a balanced
                       JE tagged `treasury_receipt`.
  * `pay`            — دفع. On a vendor bill → delegates to
                       `vendor_bills.record_bill_payment`. On misc → posts
                       `treasury_payment`. Guards against overdraft — throws
                       `TreasuryError('insufficient_funds', ...)` unless
                       the caller passes `confirm_overdraft=True` (AC #5,
                       warning-not-block).
  * `transfer`       — تحويل. Delegates to the existing
                       `accounting_ops.transfer` operation so the JE +
                       source_type (`money_transfer`) match the
                       today's `/accounting-ops/transfer` output (AC #10).

Phase 2 will add cheques. This file deliberately does NOT define a
Cheque model or cheque service — that ticket ships separately.
"""
from datetime import date
from app import db
from app.models import (
    Account, Company, Invoice, VendorBill, PaymentMethod,
)
from app.services.ledger import (
    LedgerError, post_journal, cash_and_bank_accounts,
    resolve_financial_account, get_account_by_code,
)


class TreasuryError(Exception):
    """Treasury-specific validation failure. Route layer catches this
    and re-flashes with the message. Codes:
      * `insufficient_funds` — pay > balance without confirm_overdraft.
      * `missing_target`     — invoice_id / vendor_bill_id absent.
      * `wrong_source`       — invoice on another company, or a bill
                                that is fully paid.
    """
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ─── Read side ─────────────────────────────────────────────────────
def list_balances(company_id):
    """Every cash / bank account for the company grouped as
    `[(group_label_ar, [{account, balance}, ...]), ...]`.

    Reuses `cash_and_bank_accounts` — same rules the wizard uses (only
    postable leaves, banks walked via parent_id from 1120, active only)
    so the dashboard, the receive modal, and the pay modal all see the
    exact same list. Live balance is `Account.balance` (already excludes
    paused JEs)."""
    groups = []
    for label, accs in cash_and_bank_accounts(company_id):
        rows = [{
            "account": a,
            "balance": float(a.balance or 0),
        } for a in accs]
        groups.append((label, rows))
    return groups


def kpi(company_id):
    """One pass over the same grouped list — surface totals for the
    KPI card row + the dashboard tile."""
    groups = list_balances(company_id)
    total_cash = 0.0
    total_bank = 0.0
    n = 0
    for label, rows in groups:
        subtotal = sum(r["balance"] for r in rows)
        n += len(rows)
        if label == "الصندوق":
            total_cash += subtotal
        else:
            total_bank += subtotal
    return {
        "total_cash": round(total_cash, 2),
        "total_bank": round(total_bank, 2),
        "combined": round(total_cash + total_bank, 2),
        "account_count": n,
    }


# ─── Payment-method resolver ───────────────────────────────────────
def _pm_for_account(company_id, account_id):
    """Find or auto-provision a PaymentMethod whose account_id matches.

    Every existing invoice-collection / vendor-bill-payment flow uses a
    `payment_method_id`. Treasury lets the user pick a raw Account
    (الصندوق / بنك CIB / …) — so we lift the picked account into a
    PaymentMethod so the shared services can consume it unchanged. Idempotent:
    a second treasury op on the same account reuses the existing row.
    """
    pm = (PaymentMethod.query
          .filter_by(company_id=company_id, account_id=account_id)
          .first())
    if pm:
        # Reactivate if the owner disabled it — treasury operating on
        # that account is an implicit "yes, this is a real payment
        # channel". No accounting side-effect from is_active.
        if not pm.is_active:
            pm.is_active = True
            db.session.flush()
        return pm
    acc = db.session.get(Account, account_id)
    if not acc or acc.company_id != company_id:
        raise TreasuryError("wrong_source", "الحساب المالي غير صالح")
    name = acc.name_ar or acc.name or f"Account #{acc.id}"
    pm = PaymentMethod(
        company_id=company_id,
        name=name,
        name_ar=acc.name_ar,
        account_id=acc.id,
        is_active=True,
        is_default=False,
    )
    db.session.add(pm)
    db.session.flush()
    return pm


# ─── Receive (قبض) ─────────────────────────────────────────────────
def receive(company_id, *, amount, account_id, source,
            invoice_id=None, note=None, actor_id=None):
    """Record incoming money.

      * `source="invoice"` + `invoice_id` → delegates to
        `record_payment` so invoice status, Payment row and commission
        firing behave identically to `/invoices/<id>/pay` (AC #2 — no
        duplicate accounting path).
      * `source="misc"` → posts a balanced JE tagged
        `treasury_receipt` (Dr account / Cr 4500 إيرادات أخرى) with
        `note` as the description (AC #4).

    Returns the JournalEntry on success. Raises TreasuryError /
    LedgerError on failure — the route flashes those.
    """
    from app.services.invoicing import record_payment  # avoid circular

    amt = float(amount or 0)
    if amt <= 0:
        raise TreasuryError("bad_amount", "المبلغ يجب أن يكون أكبر من صفر")
    acc, _label = resolve_financial_account(company_id, account_id)

    if source == "invoice":
        if not invoice_id:
            raise TreasuryError("missing_target", "اختر الفاتورة")
        inv = db.session.get(Invoice, int(invoice_id))
        if not inv or inv.company_id != company_id:
            raise TreasuryError("wrong_source", "الفاتورة غير موجودة")
        pm = _pm_for_account(company_id, acc.id)
        # record_payment handles balance-check + status flip +
        # commission firing internally.
        record_payment(inv, amt,
                        payment_method_id=pm.id,
                        created_by=actor_id)
        return None  # entry created inside record_payment

    if source == "misc":
        income_acc = get_account_by_code(company_id, "4500")
        if not income_acc:
            raise TreasuryError(
                "coa_missing",
                "حساب الإيرادات الأخرى (4500) غير موجود في دليل الحسابات")
        desc = (note or "").strip() or "قبض عام"
        return post_journal(
            company_id=company_id,
            description=desc,
            lines=[
                {"account_id": acc.id, "debit": amt, "credit": 0},
                {"account_id": income_acc.id, "debit": 0, "credit": amt},
            ],
            entry_date=date.today(),
            created_by=actor_id,
            source_type="treasury_receipt",
        )

    raise TreasuryError("bad_source", f"نوع القبض غير معروف: {source}")


# ─── Pay (دفع) ─────────────────────────────────────────────────────
def pay(company_id, *, amount, account_id, source,
        vendor_bill_id=None, note=None,
        confirm_overdraft=False, actor_id=None):
    """Record outgoing money. Mirror of `receive`:

      * `source="vendor_bill"` → `record_bill_payment` (AC #3).
      * `source="misc"` → JE Dr 5910 مصروفات متنوعة / Cr account
        tagged `treasury_payment`.

    Overdraft guard (AC #5): if `account.balance < amt` and
    `confirm_overdraft` is False → `TreasuryError("insufficient_funds", …)`.
    The route catches that, re-renders the modal with "الرصيد X — تأكيد
    السحب؟", and the user resubmits with `confirm_overdraft=1`.
    """
    from app.services.vendor_bills import record_bill_payment

    amt = float(amount or 0)
    if amt <= 0:
        raise TreasuryError("bad_amount", "المبلغ يجب أن يكون أكبر من صفر")
    acc, label = resolve_financial_account(company_id, account_id)

    available = float(acc.balance or 0)
    if amt > available + 0.005 and not confirm_overdraft:
        raise TreasuryError(
            "insufficient_funds",
            f"الرصيد المتاح في {label}: {available:.2f} — "
            f"المبلغ المطلوب {amt:.2f}. أكّد السحب للمتابعة."
        )

    if source == "vendor_bill":
        if not vendor_bill_id:
            raise TreasuryError("missing_target", "اختر فاتورة المورد")
        bill = db.session.get(VendorBill, int(vendor_bill_id))
        if not bill or bill.company_id != company_id:
            raise TreasuryError("wrong_source", "فاتورة المورد غير موجودة")
        pm = _pm_for_account(company_id, acc.id)
        record_bill_payment(bill, amt,
                             payment_method_id=pm.id,
                             created_by=actor_id)
        return None

    if source == "misc":
        # 5910 = مصروفات متنوعة (seeded). Fall back to any 59xx if not.
        exp_acc = (get_account_by_code(company_id, "5910")
                   or get_account_by_code(company_id, "5900"))
        if not exp_acc:
            raise TreasuryError(
                "coa_missing",
                "حساب المصروفات المتنوعة غير موجود في دليل الحسابات")
        desc = (note or "").strip() or "دفع عام"
        return post_journal(
            company_id=company_id,
            description=desc,
            lines=[
                {"account_id": exp_acc.id, "debit": amt, "credit": 0},
                {"account_id": acc.id, "debit": 0, "credit": amt},
            ],
            entry_date=date.today(),
            created_by=actor_id,
            source_type="treasury_payment",
        )

    raise TreasuryError("bad_source", f"نوع الدفع غير معروف: {source}")


# ─── Transfer (تحويل) ──────────────────────────────────────────────
def transfer(company_id, *, from_id, to_id, amount, note=None,
             actor_id=None):
    """Move money between two of the tenant's cash/bank accounts.

    Delegates to the existing `accounting_ops.transfer` operation so
    the JE, source_type (`money_transfer`), cashflow_category
    (`NONCASH` — an internal move), and description shape all match
    the today's `/accounting-ops/transfer` output. AC #10 satisfied.
    """
    from app.services.accounting_ops import get_operation, run_operation
    op = get_operation("transfer")
    if not op:
        raise TreasuryError("op_missing",
                             "عملية التحويل غير مسجّلة في النظام")
    return run_operation(op, company_id, {
        "amount": amount,
        "date": date.today().isoformat(),
        "account_id": from_id,
        "account_id_to": to_id,
        "notes": note or "",
    }, actor_id=actor_id)
