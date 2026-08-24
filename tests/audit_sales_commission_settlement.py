#!/usr/bin/env python3
"""MARSOUD-COMM-SETTLE (2026-08-25) — commission accrual vs 2150.

The invariant this ticket exists to protect:

    what is still owed on sales_commissions  ==  the 2150 balance

`2150 عمولات مبيعات مستحقة` is credited when a commission accrues and
debited when it is paid — through a payroll run, or through a manual
cash settlement. If the two sides ever disagree, either a commission was
paid without closing its liability (the reported bug: 2150 reached 8000
on company 8 while 4800 had actually gone out), or a liability was closed
against nothing.

DETECTION ONLY. This script never posts a journal and never edits a row;
a wrong balance is a judgement call for whoever reads it, not something
to auto-correct on live books.

Exit code 0 = every company reconciles.
"""
import os
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db          # noqa: E402

TOLERANCE = 0.01                        # one piastre of FP slack


def _p(msg):
    """ASCII-safe print — Windows cp1252 stdout dies on box glyphs."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode())


def account_balance(company_id, code):
    """Signed GL balance of `code` (debits - credits), active entries only."""
    from app.models import JournalLine, JournalEntry
    from app.services.ledger import get_account_by_code
    acc = get_account_by_code(company_id, code)
    if not acc:
        return None
    rows = (db.session.query(JournalLine)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .filter(JournalLine.account_id == acc.id,
                    JournalEntry.is_active.is_(True))
            .all())
    return round(sum(float(r.debit or 0) - float(r.credit or 0)
                     for r in rows), 2)


def main():
    app = create_app()
    failures = []
    checked = 0

    with app.app_context():
        from app.models import Company, SalesCommission

        _p("MARSOUD-COMM-SETTLE - commission settlement reconciliation")
        _p("=" * 72)

        for co in Company.query.order_by(Company.id).all():
            rows = SalesCommission.query.filter_by(company_id=co.id).all()
            bal2150 = account_balance(co.id, "2150")

            if not rows and not bal2150:
                continue                       # nothing to say about it
            checked += 1

            # 2150 is a LIABILITY: credits are negative in signed terms,
            # so what is owed is the negated balance.
            owed_gl = round(-(bal2150 or 0), 2)
            owed_rows = round(sum(r.remaining for r in rows), 2)
            drift = round(owed_rows - owed_gl, 2)

            name = (co.name or "")[:26]
            status = "OK" if abs(drift) <= TOLERANCE else "DRIFT"
            _p("  [%-5s] company %-4s %-26s rows=%-9.2f 2150=%-9.2f "
               "drift=%.2f" % (status, co.id, name, owed_rows, owed_gl, drift))

            if abs(drift) > TOLERANCE:
                failures.append(
                    "company %s (%s): commissions still owed %.2f but 2150 "
                    "carries %.2f - drift %.2f"
                    % (co.id, name, owed_rows, owed_gl, drift))

            # A settled row must point at the journal that settled it.
            orphan_settled = [
                r for r in rows
                if r.settled_at is not None and not r.payroll_run_id
                and not _has_manual_settlement(r)
            ]
            if orphan_settled:
                failures.append(
                    "company %s: %d settled commission row(s) with no "
                    "settling journal (ids %s)"
                    % (co.id, len(orphan_settled),
                       [r.id for r in orphan_settled][:5]))

            # Nothing may be settled for more than it was worth.
            over = [r for r in rows
                    if float(r.settled_amount or 0) - float(r.amount or 0)
                    > TOLERANCE]
            if over:
                failures.append(
                    "company %s: %d commission row(s) settled for MORE than "
                    "their amount (ids %s)"
                    % (co.id, len(over), [r.id for r in over][:5]))

        _p("")
        _p("companies with commission activity: %d" % checked)
        if failures:
            _p("FAIL - %d finding(s):" % len(failures))
            for f in failures:
                _p("  - " + f)
            return 1
        _p("OK - every company's open commissions tie to its 2150 balance")
        return 0


def _has_manual_settlement(row):
    """True when a commission_settle journal exists for this row."""
    from app.models import JournalEntry
    return db.session.query(
        JournalEntry.query.filter_by(
            source_type="commission_settle", source_id=row.id,
        ).exists()).scalar()


if __name__ == "__main__":
    sys.exit(main())
