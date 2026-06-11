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
    "VENDOR_BILL": "VB",
    "POS": "POS",   # ERP-02 — POS orders
    "STOCK_TRANSFER": "TR",  # ERP-03 — warehouse transfers
}


def next_number(company_id, doc_type, width=4):
    """Atomically claim and return the next formatted document number.

    Returns a string like "INV-0001". Creates the sequence row on first use.
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

    n = seq.next_number
    seq.next_number = n + 1
    db.session.flush()

    return f"{prefix}-{n:0{width}d}"


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
