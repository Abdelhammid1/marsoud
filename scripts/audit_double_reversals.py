#!/usr/bin/env python3
"""MARSOUD-DOUBLE-REVERSAL-DIAG (2026-08-06) — read-only report on
journal entries that carry more than one active reversal.

The MARSOUD-REVERSE-ONCE guard (services/ledger.py) prevents new
double-reversals from being posted. It does NOT undo the ones that
predate the guard. This script counts them per company so we can
decide, per case, whether to reverse the extra reversal, delete it,
or leave it documented — that decision is out of scope here.

STRICT INVARIANTS
=================
Read-only. Absolutely no write path. If you're editing this file and
find yourself typing db.session.add / merge / delete / commit, stop —
the diagnostic loses its value the moment it can mutate. The audit
suite pins this: two consecutive runs must produce byte-identical
output, and every row in journal_entries, journal_lines,
open_items, open_item_settlements, customer_deposits must survive
unchanged.

Usage:
    flask audit-double-reversals                    # all companies
    flask audit-double-reversals --company-id 8     # one company

Exit code is 0 whether or not duplicates are found — this is a
report, not a check. Non-zero would break scheduled CI runs and
teach people to ignore the output.
"""
import click
from flask.cli import with_appcontext
from sqlalchemy import func

from app import db
from app.models import JournalEntry, JournalLine, Company


def _find_duplicates(company_id=None):
    """Return {company_id: [(original_entry, [reversal_entries])]}.

    Read-only query. Groups active reversals by their reversal_of
    target and keeps only groups with N > 1 — i.e., an original that
    has been reversed more than once and the extra reversal(s) still
    live.
    """
    q = db.session.query(
        JournalEntry.reversal_of,
        func.count(JournalEntry.id).label("n"),
    ).filter(
        JournalEntry.reversal_of.isnot(None),
        JournalEntry.is_active.is_(True),
    )
    if company_id is not None:
        q = q.filter(JournalEntry.company_id == company_id)
    q = q.group_by(JournalEntry.reversal_of).having(
        func.count(JournalEntry.id) > 1)
    dup_originals = [row for row in q.all()]

    out = {}
    for orig_id, n in dup_originals:
        original = db.session.get(JournalEntry, orig_id)
        if original is None:
            # An ORPHAN reversal — a rare data-fix artefact. Bucket
            # it under company_id=None so it still surfaces.
            cid = None
        else:
            cid = original.company_id
        reversals = (JournalEntry.query
                     .filter_by(reversal_of=orig_id, is_active=True)
                     .order_by(JournalEntry.date.asc(),
                                JournalEntry.id.asc()).all())
        out.setdefault(cid, []).append((original, reversals))
    return out


def _entry_debit_total(entry):
    """Sum of debit lines for one entry. Debits and credits balance, so
    either side gives us the same number; debit is the convention
    for reporting."""
    if entry is None:
        return 0.0
    total = db.session.query(
        func.coalesce(func.sum(JournalLine.debit), 0)
    ).filter(JournalLine.entry_id == entry.id).scalar()
    return float(total or 0)


def _print_report(by_company, scope_label):
    print("\n" + "-" * 62)
    print(f"double-reversal diagnostic — {scope_label}")
    print("-" * 62)

    if not by_company:
        print("no duplicate reversals found ✓")
        print()
        return

    grand_total_extra = 0.0
    for cid, groups in sorted(by_company.items(),
                              key=lambda kv: (kv[0] is None, kv[0] or 0)):
        co = db.session.get(Company, cid) if cid is not None else None
        co_label = f"company {cid}" + (f" ({co.name})" if co else "")
        if cid is None:
            co_label = "orphan reversals (original entry missing)"
        print(f"\n{co_label}  -  {len(groups)} originals with duplicate reversals")

        company_extra = 0.0
        for original, reversals in groups:
            orig_num = ((original.number or f"#{original.id}") if original
                        else f"#{'?'}")
            src = (original.source_type or "manual") if original else "?"
            orig_amt = _entry_debit_total(original)
            print(f"  original {orig_num}  "
                  f"(source_type={src}, {orig_amt:,.2f})")
            for i, rev in enumerate(reversals):
                rev_num = rev.number or f"#{rev.id}"
                mark = "  <- duplicate" if i > 0 else ""
                rev_amt = _entry_debit_total(rev)
                print(f"    reversal {rev_num}  {rev.date}  "
                      f"{rev_amt:,.2f}{mark}")
                if i > 0:
                    company_extra += rev_amt

        print(f"  " + "-" * 32)
        print(f"  extra impact: {company_extra:,.2f}")
        grand_total_extra += company_extra

    print("\n" + "=" * 62)
    print(f"grand total extra impact across scope: {grand_total_extra:,.2f}")
    print()


@click.command("audit-double-reversals")
@click.option("--company-id", type=int, default=None,
              help="Limit to one company (default: all).")
@with_appcontext
def audit_cli(company_id):
    """Report journal entries that carry more than one active reversal.

    Read-only. This command has no write path — the ticket is explicit:
    diagnosis first, decision after.
    """
    by_company = _find_duplicates(company_id=company_id)
    scope = f"company {company_id}" if company_id else "all companies"
    _print_report(by_company, scope)
