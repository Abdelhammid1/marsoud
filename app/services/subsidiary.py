"""MARSOUD-COA-REBUILD — subsidiary-ledger helpers.

Every customer, vendor, and employee owns a child account under its
parent header (1130, 2110, 2130). This module provides:

  create_party_subaccount(company_id, parent_code, party_name) -> Account
      Inserts a new leaf under the named header, with a 6-digit
      sequential suffix (e.g. 1130-000017). Never reuses numbers.

  ensure_customer_account(customer) / ensure_vendor_account(vendor) /
  ensure_employee_account(employee)
      Idempotent: returns the party's existing account, or creates one
      if account_id is NULL. Called lazily from posting code so legacy
      rows (pre-rebuild) auto-heal on first transaction.

  party_ar_account(invoice) / party_ap_account(bill) /
  party_payroll_account(employee)
      Convenience helpers used by invoicing/vendor_bills/payroll for
      the AR / AP / Salaries-Payable leg of each journal.
"""
from app import db
from app.models import Account, AccountType
from app.models.account import NORMAL_SIDE_FOR_TYPE


# Spec from the ticket — 6-digit suffix is wide enough that we never
# run out + collision-free even after deletes.
_SUFFIX_WIDTH = 6


# MARSOUD-CUSTODY-BUGS-02 (2026-08-08) — well-known party-header
# codes we auto-mint if a legacy tenant's COA is missing them.
# Each has a canonical (name_en, name_ar, type) documented in
# seed_coa.py, and a canonical grandparent (1100 / 2100). Any
# other parent code still raises loudly — a typo in a caller's
# parent_code arg is a real bug, not a tenant-state issue.
_KNOWN_PARTY_HEADERS = {
    "1130": ("Trade Receivables", "العملاء — المدينون", AccountType.ASSET),
    "1180": ("Cash Custody in Settlement", "عهد نقدية تحت التسوية",
             AccountType.ASSET),
    "2110": ("Trade Payables", "الموردون — الدائنون",
             AccountType.LIABILITY),
    "2130": ("Salaries Payable", "الرواتب المستحقة",
             AccountType.LIABILITY),
}
_KNOWN_PARENT_OF = {
    "1130": "1100", "1180": "1100",
    "2110": "2100", "2130": "2100",
}


def _lazy_create_known_header(company_id, parent_code):
    """MARSOUD-CUSTODY-BUGS-02 (2026-08-08) — mint a missing
    party-header account for legacy tenants whose COA was seeded
    before the header was added to seed_default_coa. Called only
    when the header is one of the four documented codes; any other
    code falls through to the raise below.

    Companion to the runtime migration; this layer covers a
    tenant whose super-admin manually deleted the header, or a
    mid-migration state that the alembic upgrade hasn't reached
    yet. Both paths land in a byte-identical Account row —
    parent_id derived from the same 1100 / 2100 the seed uses,
    normal_side from NORMAL_SIDE_FOR_TYPE.
    """
    name_en, name_ar, type_ = _KNOWN_PARTY_HEADERS[parent_code]
    grandparent_code = _KNOWN_PARENT_OF[parent_code]
    grandparent = Account.query.filter_by(
        company_id=company_id, code=grandparent_code,
    ).first()
    if grandparent is None:
        # If the tenant is missing BOTH the header and its
        # grandparent, we point at both codes so the accountant
        # doesn't fix one and then be told about the other.
        raise ValueError(
            f"الحساب الأب {parent_code} وحسابه الأب "
            f"{grandparent_code} غير موجودين — راجع شجرة الحسابات"
        )
    parent = Account(
        company_id=company_id,
        code=parent_code,
        name=name_en, name_ar=name_ar,
        type=type_,
        parent_id=grandparent.id,
        is_postable=False,
        normal_side=NORMAL_SIDE_FOR_TYPE[type_],
        is_active=True,
    )
    db.session.add(parent)
    db.session.flush()
    return parent


def create_party_subaccount(company_id, parent_code, party_name):
    """Create a leaf account under the header coded `parent_code`."""
    parent = Account.query.filter_by(
        company_id=company_id, code=parent_code,
    ).first()
    if not parent and parent_code in _KNOWN_PARTY_HEADERS:
        parent = _lazy_create_known_header(company_id, parent_code)
    if not parent:
        raise ValueError(
            f"الحساب الأب {parent_code} غير موجود — راجع شجرة الحسابات"
        )

    # Walk the existing siblings to find the next free sequence number.
    prefix = f"{parent_code}-"
    siblings = Account.query.filter(
        Account.company_id == company_id,
        Account.code.like(f"{prefix}%"),
    ).all()
    max_seq = 0
    for s in siblings:
        try:
            seq = int(s.code[len(prefix):])
            if seq > max_seq:
                max_seq = seq
        except (ValueError, TypeError):
            continue

    new_code = f"{prefix}{max_seq + 1:0{_SUFFIX_WIDTH}d}"
    acc = Account(
        company_id=company_id,
        code=new_code,
        name=party_name,
        name_ar=party_name,
        type=parent.type,
        normal_side=NORMAL_SIDE_FOR_TYPE[parent.type],
        parent_id=parent.id,
        is_postable=True,   # leaves accept journal lines
    )
    db.session.add(acc)
    db.session.flush()
    return acc


# ─── Idempotent per-party helpers ───────────────────────────────────────
def ensure_customer_account(customer):
    """Return the customer's AR sub-account; create one if absent."""
    if customer.account_id:
        return customer.account
    acc = create_party_subaccount(customer.company_id, "1130", customer.name)
    customer.account_id = acc.id
    db.session.flush()
    return acc


def ensure_vendor_account(vendor):
    """Return the vendor's AP sub-account; create one if absent."""
    if vendor.account_id:
        return vendor.account
    acc = create_party_subaccount(vendor.company_id, "2110", vendor.name)
    vendor.account_id = acc.id
    db.session.flush()
    return acc


def ensure_employee_account(employee):
    """Return the employee's Salaries-Payable sub-account; create if absent."""
    if employee.account_id:
        return employee.account
    acc = create_party_subaccount(employee.company_id, "2130", employee.name)
    employee.account_id = acc.id
    db.session.flush()
    return acc


def ensure_custody_account(holder):
    """MARSOUD-CASH-CUSTODY-01 (2026-08-07) — return the custody
    holder's Cash-Custody sub-account under 1180; create if absent.

    Works for BOTH Employee AND Department (the two allowed holder
    types per the ticket). Both models carry a `custody_account_id`
    FK — the helper duck-types on it. Idempotent; safe to call at
    every issue journal.

    The 1180-NNNNNN leaves let the party-ledger + open-custody
    reports slice by holder without another table."""
    if holder.custody_account_id:
        return holder.custody_account
    acc = create_party_subaccount(holder.company_id, "1180", holder.name)
    holder.custody_account_id = acc.id
    db.session.flush()
    return acc


WALK_IN_CUSTOMER_NAME = "زبون نقدي (Walk-in)"


def ensure_walk_in_customer(company_id):
    """Return the company's singleton 'walk-in' Customer record, creating
    it (+ its sub-account) on first call. Used by POS for invoices
    without a customer_id so every transaction still lands on a real
    customer ledger."""
    from app.models import Customer
    walk = Customer.query.filter_by(
        company_id=company_id, name=WALK_IN_CUSTOMER_NAME,
    ).first()
    if walk:
        if not walk.account_id:
            ensure_customer_account(walk)
        return walk
    walk = Customer(
        company_id=company_id,
        name=WALK_IN_CUSTOMER_NAME,
        email=None, phone=None, is_active=True,
    )
    db.session.add(walk); db.session.flush()
    ensure_customer_account(walk)
    return walk


# ─── Posting-side helpers ───────────────────────────────────────────────
def party_ar_account(invoice):
    """For invoicing — the AR sub-account for the invoice's customer.
    When the invoice has no customer (POS walk-in), the per-company
    'زبون نقدي' Customer is used so the journal still has a real party."""
    customer = invoice.customer
    if customer is None:
        customer = ensure_walk_in_customer(invoice.company_id)
        invoice.customer_id = customer.id
    return ensure_customer_account(customer)


def party_ap_account(bill):
    """For vendor_bills — the AP sub-account for the bill's vendor."""
    return ensure_vendor_account(bill.vendor)


def party_payroll_account(employee):
    """For payroll — the Salaries-Payable sub-account for one employee."""
    return ensure_employee_account(employee)


def party_custody_account(holder):
    """MARSOUD-CASH-CUSTODY-01 — the 1180-NNNNNN sub-account for
    the holder (Employee or Department) of a cash custody. Used
    by services/cash_custody on both the issue journal (Dr this
    account / Cr cash) and the close-settlement journal (Cr this
    account for the settled+returned+shortfall)."""
    return ensure_custody_account(holder)


# ─── MARSOUD-PARTY-OPENING-BALANCE-01 ──────────────────────────────────
def _has_party_activity(party_type, party):
    """True if this party already has posted transactions. Mirrors the
    inventory record_opening_balance() rule: opening balance is one-shot
    at coding time, refused if any real activity exists."""
    from app.models import (
        Invoice, InvoiceStatus, VendorBill, PartyOpeningBalance, PartyType,
    )
    if party_type == PartyType.CUSTOMER:
        # ANY invoice (draft or posted) counts as activity — an accountant
        # who's built a draft invoice implicitly says "this party has
        # started transacting, opening balance no longer applies."
        return Invoice.query.filter_by(customer_id=party.id).first() is not None
    if party_type == PartyType.VENDOR:
        return VendorBill.query.filter_by(vendor_id=party.id).first() is not None
    return False


def _existing_opening(company_id, party_type, party_id):
    from app.models import PartyOpeningBalance
    return PartyOpeningBalance.query.filter_by(
        company_id=company_id, party_type=party_type,
        party_id=party_id,
    ).first()


def record_customer_opening_balance(customer, amount, *,
                                     entry_date=None, created_by=None):
    """One-shot: record an opening receivable for `customer`.

    Journal:  Dr customer-sub (under 1130)   / Cr 3900 (Opening Equity)
    Negative amount reverses direction (customer sitting on an advance).

    Refuses if:
      - the customer already has any invoice
      - an opening balance row already exists for this party
    Silently no-ops when amount is exactly 0 (matches the ticket spec —
    "لو اتسيب صفر، مفيش أي قيد بيتعمل خالص")."""
    from datetime import date as _date
    from app.models import PartyOpeningBalance, PartyType
    from app.services.ledger import post_journal, get_account_by_code, LedgerError

    if amount is None or abs(float(amount)) < 0.001:
        return None
    if _has_party_activity(PartyType.CUSTOMER, customer):
        raise LedgerError(
            "لا يمكن إدخال رصيد افتتاحي — العميل عنده فواتير بالفعل."
        )
    if _existing_opening(customer.company_id,
                          PartyType.CUSTOMER, customer.id):
        raise LedgerError(
            "الرصيد الافتتاحي مسجل بالفعل لهذا العميل."
        )
    ar = ensure_customer_account(customer)
    open_acc = get_account_by_code(customer.company_id, "3900")
    if not open_acc:
        raise LedgerError(
            "حساب الافتتاح (3900) غير موجود — راجع شجرة الحسابات"
        )
    entry_date = entry_date or _date.today()
    amt = float(amount)
    if amt > 0:
        # Customer owes us — Dr AR sub / Cr 3900
        lines = [
            {"account_id": ar.id, "debit": amt, "credit": 0,
             "memo": f"رصيد افتتاحي — {customer.name}"},
            {"account_id": open_acc.id, "debit": 0, "credit": amt,
             "memo": "حساب الافتتاح"},
        ]
    else:
        # Customer sitting on an advance — reverse both sides.
        amt_abs = -amt
        lines = [
            {"account_id": open_acc.id, "debit": amt_abs, "credit": 0,
             "memo": "حساب الافتتاح"},
            {"account_id": ar.id, "debit": 0, "credit": amt_abs,
             "memo": f"رصيد افتتاحي (مقدم) — {customer.name}"},
        ]
    entry = post_journal(
        company_id=customer.company_id,
        description=f"رصيد افتتاحي — العميل {customer.name}",
        lines=lines,
        entry_date=entry_date,
        reference=f"OB-C-{customer.id}",
        currency=customer.company.base_currency
                    if customer.company else None,
        created_by=created_by,
        source_type="party_opening_balance",
        source_id=customer.id,
    )
    ob = PartyOpeningBalance(
        company_id=customer.company_id,
        party_type=PartyType.CUSTOMER,
        party_id=customer.id,
        amount=amt,
        entry_date=entry_date,
        journal_entry_id=entry.id,
        created_by=created_by,
    )
    db.session.add(ob)
    db.session.flush()
    return ob


def record_vendor_opening_balance(vendor, amount, *,
                                    entry_date=None, created_by=None):
    """One-shot: record an opening payable to `vendor`.

    Journal:  Dr 3900 (Opening Equity)         / Cr vendor-sub (under 2110)
    Negative amount reverses direction (we prepaid the vendor)."""
    from datetime import date as _date
    from app.models import PartyOpeningBalance, PartyType
    from app.services.ledger import post_journal, get_account_by_code, LedgerError

    if amount is None or abs(float(amount)) < 0.001:
        return None
    if _has_party_activity(PartyType.VENDOR, vendor):
        raise LedgerError(
            "لا يمكن إدخال رصيد افتتاحي — المورد عنده فواتير بالفعل."
        )
    if _existing_opening(vendor.company_id,
                          PartyType.VENDOR, vendor.id):
        raise LedgerError(
            "الرصيد الافتتاحي مسجل بالفعل لهذا المورد."
        )
    ap = ensure_vendor_account(vendor)
    open_acc = get_account_by_code(vendor.company_id, "3900")
    if not open_acc:
        raise LedgerError(
            "حساب الافتتاح (3900) غير موجود — راجع شجرة الحسابات"
        )
    entry_date = entry_date or _date.today()
    amt = float(amount)
    if amt > 0:
        # We owe the vendor — Dr 3900 / Cr AP sub
        lines = [
            {"account_id": open_acc.id, "debit": amt, "credit": 0,
             "memo": "حساب الافتتاح"},
            {"account_id": ap.id, "debit": 0, "credit": amt,
             "memo": f"رصيد افتتاحي — {vendor.name}"},
        ]
    else:
        # We prepaid the vendor — reverse both sides.
        amt_abs = -amt
        lines = [
            {"account_id": ap.id, "debit": amt_abs, "credit": 0,
             "memo": f"رصيد افتتاحي (مقدم) — {vendor.name}"},
            {"account_id": open_acc.id, "debit": 0, "credit": amt_abs,
             "memo": "حساب الافتتاح"},
        ]
    entry = post_journal(
        company_id=vendor.company_id,
        description=f"رصيد افتتاحي — المورد {vendor.name}",
        lines=lines,
        entry_date=entry_date,
        reference=f"OB-V-{vendor.id}",
        currency=vendor.company.base_currency
                    if vendor.company else None,
        created_by=created_by,
        source_type="party_opening_balance",
        source_id=vendor.id,
    )
    ob = PartyOpeningBalance(
        company_id=vendor.company_id,
        party_type=PartyType.VENDOR,
        party_id=vendor.id,
        amount=amt,
        entry_date=entry_date,
        journal_entry_id=entry.id,
        created_by=created_by,
    )
    db.session.add(ob)
    db.session.flush()
    return ob
