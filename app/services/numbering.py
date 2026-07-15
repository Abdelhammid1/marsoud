"""Per-company document numbering.

Each company has its own counter for each document type, so Company A's
INV-0001 and Company B's INV-0001 can coexist independently.

Usage:
    from app.services.numbering import next_number
    inv_no = next_number(company_id, "INVOICE")   # → "INV-0001"
"""
from app import db
from app.models import NumberSequence


DOC_PREFIXES = {
    "INVOICE": "INV",
    "JOURNAL": "JE",
    "PAYROLL": "PAYROLL",
    "EMPLOYEE": "EMP",
    "PAYMENT": "PMT",
    "REFUND": "REF",
    "CREDIT_NOTE": "CN",
    # MARSOUD-REFUNDS-01 — dedicated sequences for sales/purchase refunds.
    # Distinct from legacy "REFUND" (payment-refund) so the audit trail
    # for goods returns stays clean.
    "SALES_REFUND": "SRET",
    "PURCHASE_REFUND": "PRET",
    "DEBIT_NOTE": "DN",
    "VENDOR_BILL": "VB",
    "POS": "POS",   # ERP-02 — POS orders
    "STOCK_TRANSFER": "TR",  # ERP-03 — warehouse transfers
    "MANUFACTURING_ORDER": "MO",  # MARSOUD-MANUFACTURING-01
    # PER-CO-NUMBERING (Abdelhamid 2026-07-04) — every company counts
    # its own leads / projects / product SKUs from 1. Previously the
    # UI leaked the global DB id (e.g. "عميل محتمل #92" on a fresh
    # company where the user expected #1).
    "LEAD": "L",
    "PROJECT": "PRJ",
    "PRODUCT": "PRD",
}


def next_number(company_id, doc_type, width=4):
    """Atomically claim and return the next formatted document number.

    Returns a string like "INV-0001". Creates the sequence row on first use.

    MARSOUD-BUG-EMP-NUM (2026-07): Abdelhamid reported new employees in a
    freshly created company getting a number in the 30s instead of
    EMP-0001. The `NumberSequence` table on his server had a row for the
    new company_id already sitting at a high `next_number` (imported /
    migrated / manually seeded). To heal without touching his DB by
    hand, we now sanity-check the seq against the actual max number
    already used inside the company — if `seq.next_number` is above
    (actual_max + 1) it silently resyncs downwards. Never resyncs
    upwards (that would risk collisions) and only applies to doc types
    where re-syncing is safe (EMPLOYEE for now — invoices/journals need
    ordered gap-free numbering).
    """
    prefix = DOC_PREFIXES.get(doc_type)
    if not prefix:
        raise ValueError(f"Unknown doc_type: {doc_type}")

    seq = NumberSequence.query.filter_by(
        company_id=company_id, doc_type=doc_type
    ).first()

    if not seq:
        seq = NumberSequence(company_id=company_id, doc_type=doc_type, next_number=1)
        db.session.add(seq)
        db.session.flush()

    # Self-heal for EMPLOYEE + LEAD. If the seq is way ahead of what's
    # actually in the DB (e.g. legacy shared counter) drop it back down.
    # MARSOUD-LEAD-NUM-RESYNC (Abdelhamid 2026-07-15) — he reported a
    # brand-new company's leads showing L-0105 when barely any leads
    # existed. Same shape as the earlier EMP-NUM bug. Lead numbers
    # aren't ledger-critical so shrinking is safe. Invoice/journal
    # numbering stays gap-free.
    if doc_type in ("EMPLOYEE", "LEAD") and seq.next_number > 1:
        _resync_if_stale(seq, company_id, prefix, width, doc_type=doc_type)

    n = seq.next_number
    seq.next_number = n + 1
    db.session.flush()

    return f"{prefix}-{n:0{width}d}"


def _resync_if_stale(seq, company_id, prefix, width, doc_type="EMPLOYEE"):
    """Look at the max <prefix>-NNNN actually used in this company. If
    the seq is above (max + 1), quietly reset it. Never moves the
    counter up."""
    from app.models import Employee, Lead
    actual_max = 0
    if doc_type == "EMPLOYEE":
        rows = Employee.query.filter(
            Employee.company_id == company_id,
            Employee.employee_number.isnot(None),
            Employee.employee_number.like(f"{prefix}-%"),
        ).all()
        get_num = lambda r: r.employee_number
    elif doc_type == "LEAD":
        rows = Lead.query.filter(
            Lead.company_id == company_id,
            Lead.number.isnot(None),
            Lead.number.like(f"{prefix}-%"),
        ).all()
        get_num = lambda r: r.number
    else:
        return
    for row in rows:
        try:
            n = int((get_num(row) or "").split("-")[-1])
            actual_max = max(actual_max, n)
        except (ValueError, IndexError):
            continue
    expected_next = actual_max + 1
    if seq.next_number > expected_next:
        import logging
        logging.getLogger("ledgeros.numbering").info(
            "[%s-NUM-RESYNC] company=%s: seq.next_number was %s, "
            "actual max in company is %s → resetting to %s",
            doc_type, company_id, seq.next_number, actual_max,
            expected_next,
        )
        seq.next_number = expected_next


def peek_next_number(company_id, doc_type, width=4):
    """Show what the next number WILL be without consuming it. For previews only."""
    prefix = DOC_PREFIXES.get(doc_type)
    if not prefix:
        raise ValueError(f"Unknown doc_type: {doc_type}")
    seq = NumberSequence.query.filter_by(company_id=company_id, doc_type=doc_type).first()
    n = seq.next_number if seq else 1
    return f"{prefix}-{n:0{width}d}"


def initialize_sequence(company_id, doc_type, starting_at=1):
    """Seed a sequence to start from a specific number (useful for migrations
    that need new numbering to begin AFTER existing legacy data)."""
    seq = NumberSequence.query.filter_by(company_id=company_id, doc_type=doc_type).first()
    if not seq:
        seq = NumberSequence(company_id=company_id, doc_type=doc_type, next_number=starting_at)
        db.session.add(seq)
    else:
        seq.next_number = max(seq.next_number, starting_at)
    db.session.flush()
    return seq
