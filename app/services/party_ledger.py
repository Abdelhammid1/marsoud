"""MARSOUD-PARTY-LEDGER-02 — unified statement of account for any
customer / vendor / employee.

Reads every JournalLine posted to the party's own sub-account, in
chronological order, computes a running balance, and returns a
list of dicts ready for the template + the PDF/Excel exporters.

Public surface:
  list_parties(company_id, kind) -> list of {id, name, account_code}
  party_ledger(company_id, kind, party_id, start, end) -> dict
"""
from datetime import date
from app import db
from app.models import (
    Customer, Vendor, Employee, Account, JournalEntry, JournalLine,
    NormalSide,
)


# Map party kind → (model class, parent CoA code)
_KIND_MAP = {
    "customer": (Customer, "1130"),
    "vendor":   (Vendor,   "2110"),
    "employee": (Employee, "2130"),
}

KIND_LABELS = {
    "customer": "عميل",
    "vendor":   "مورد",
    "employee": "موظف",
}


def list_parties(company_id, kind):
    """Return every party of the given kind that has a sub-account.
    Parties without one are skipped (they have no ledger to show)."""
    if kind not in _KIND_MAP:
        raise ValueError(f"نوع طرف غير صالح: {kind}")
    Model, _parent_code = _KIND_MAP[kind]
    rows = Model.query.filter(
        Model.company_id == company_id,
        Model.account_id.isnot(None),
    ).order_by(Model.name).all()
    out = []
    for r in rows:
        acc = db.session.get(Account, r.account_id) if r.account_id else None
        if acc is None:
            continue
        out.append({
            "id": r.id,
            "name": r.name,
            "account_code": acc.code,
            "account_id": acc.id,
        })
    return out


def party_ledger(company_id, kind, party_id, start_date=None, end_date=None):
    """Build the running-balance statement for one party.

    Returns:
      {
        party:   {id, name, kind, account_code, account_id},
        rows:    [{date, entry_number, description, source_type,
                    debit, credit, balance, journal_entry_id}, ...],
        opening_balance: float,   # balance before start_date
        closing_balance: float,
        total_debit:     float,
        total_credit:    float,
        start_date / end_date,
      }
    """
    if kind not in _KIND_MAP:
        raise ValueError(f"نوع طرف غير صالح: {kind}")
    Model, parent_code = _KIND_MAP[kind]
    party = db.session.get(Model, party_id)
    if not party or party.company_id != company_id:
        raise ValueError("الطرف غير موجود")
    if not party.account_id:
        # Lazily open a sub-account so we can render an empty statement
        from app.services.subsidiary import (
            ensure_customer_account, ensure_vendor_account,
            ensure_employee_account,
        )
        opener = {
            "customer": ensure_customer_account,
            "vendor":   ensure_vendor_account,
            "employee": ensure_employee_account,
        }[kind]
        opener(party)
        db.session.flush()

    acc = db.session.get(Account, party.account_id)

    # Opening balance = signed balance of every active line BEFORE start_date
    opening_balance = 0.0
    if start_date:
        q_open = db.session.query(
            db.func.coalesce(db.func.sum(JournalLine.debit_base), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_base), 0),
        ).join(JournalEntry).filter(
            JournalLine.account_id == acc.id,
            JournalEntry.is_active.is_(True),
            JournalEntry.date < start_date,
        ).first()
        od, oc = float(q_open[0] or 0), float(q_open[1] or 0)
        if acc.normal_side == NormalSide.DEBIT:
            opening_balance = od - oc
        else:
            opening_balance = oc - od

    # Pull rows in the window
    q = db.session.query(JournalLine, JournalEntry).join(JournalEntry).filter(
        JournalLine.account_id == acc.id,
        JournalEntry.is_active.is_(True),
    )
    if start_date:
        q = q.filter(JournalEntry.date >= start_date)
    if end_date:
        q = q.filter(JournalEntry.date <= end_date)
    q = q.order_by(JournalEntry.date, JournalEntry.id, JournalLine.id)

    rows = []
    balance = opening_balance
    total_debit = 0.0
    total_credit = 0.0
    for line, entry in q.all():
        d = float(line.debit_base or 0)
        c = float(line.credit_base or 0)
        total_debit += d
        total_credit += c
        if acc.normal_side == NormalSide.DEBIT:
            balance += d - c
        else:
            balance += c - d
        rows.append({
            "date": entry.date,
            "entry_number": entry.number,
            "entry_id": entry.id,
            "description": entry.description,
            "source_type": entry.source_type or "",
            "source_id": entry.source_id,
            "memo": line.memo or "",
            "debit": d,
            "credit": c,
            "balance": round(balance, 2),
        })

    # MARSOUD-SOURCE-REFERENCE-01 (Abdelhamid 2026-07-25) — enrich
    # each row with a human-readable label + optional link to the
    # source document. Batched: 1 query per source_type per company,
    # even for 200+ rows.
    from app.services.source_reference import (
        build_reference_map, UNKNOWN_LABEL,
    )
    ref_map = build_reference_map(rows, company_id)
    for r in rows:
        r["reference"] = ref_map.get((r["source_type"], r["source_id"]),
                                       {"label": UNKNOWN_LABEL,
                                        "url": None, "kind": None})

    return {
        "party": {
            "id": party.id,
            "name": party.name,
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "account_code": acc.code,
            "account_id": acc.id,
        },
        "rows": rows,
        "opening_balance": round(opening_balance, 2),
        "closing_balance": round(balance, 2),
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "start_date": start_date,
        "end_date": end_date,
        "normal_side": acc.normal_side.value,
    }
