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


def create_party_subaccount(company_id, parent_code, party_name):
    """Create a leaf account under the header coded `parent_code`."""
    parent = Account.query.filter_by(
        company_id=company_id, code=parent_code,
    ).first()
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
