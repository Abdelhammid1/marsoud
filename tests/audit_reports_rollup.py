#!/usr/bin/env python3
"""MARSOUD-REPORTS-ROLLUP-01 — parent-account rollup in balance_sheet()
and trial_balance_report().

Before this ticket, `balance_sheet()` and `trial_balance_report()` in
`app/services/reports.py` emitted each account's own direct-post
balance only.  A grouping account like "1100 · Current Assets" whose
direct JEs are $0 (all activity posts to leaves 1110/1121/1122)
rendered as an empty parent row — the operator couldn't read the
subtotal at each hierarchy level.

This audit posts a small ledger against known-parent codes from the
seeded CoA and pins the expected rolled-up balances at every level.

Fixture: fresh company `__REPORTS_ROLLUP_AUDIT__` seeded with the
default chart of accounts, then a manual JE posts:

  Dr 1110 (Cash)          500      -- leaf under 1100
  Dr 1121 (Banque Misr)   300      -- leaf under 1120, which is under 1100
  Cr 2210 (Salaries payable) 800   -- so the JE balances

We then confirm:
  * balance_sheet: 1100 shows rolled 800, 1120 shows rolled 300,
                   1110 shows 500, 1121 shows 300, totals["assets"]
                   still 800 (own posts only, no double count).
  * trial_balance_report: same rollup shape on debit + credit
                   columns, totals["debit"] == totals["credit"] == 800.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__REPORTS_ROLLUP_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, Account, JournalEntry, JournalLine
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)
    # SQLite reuses account IDs across companies as rows are deleted +
    # inserted.  If a previous audit run left orphan JournalLines
    # (parent JournalEntry gone but the line rows survived a partial
    # teardown), those lines can attach to the new company's accounts
    # by matching the reused id — polluting the balances this audit
    # pins.  Sweep any orphan lines before we seed.
    orphan_line_ids = [r.id for r in JournalLine.query.filter(
        JournalLine.entry_id.notin_(db.session.query(JournalEntry.id))
    ).all()]
    if orphan_line_ids:
        JournalLine.query.filter(
            JournalLine.id.in_(orphan_line_ids)
        ).delete(synchronize_session=False)
        db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id

    # Post the fixture JE straight to the ledger — bypass
    # invoicing/vendor-bill helpers because we want a clean pin on
    # the account rollup, not a lifecycle side-effect.
    from app.services.ledger import post_journal
    cash = Account.query.filter_by(company_id=c.id, code="1110").one()
    bank = Account.query.filter_by(company_id=c.id, code="1121").one()
    payable = Account.query.filter_by(company_id=c.id, code="2210").one()
    post_journal(
        company_id=c.id,
        entry_date=date.today(),
        description="rollup fixture",
        lines=[
            {"account_id": cash.id,    "debit": 500.0, "credit": 0.0},
            {"account_id": bank.id,    "debit": 300.0, "credit": 0.0},
            {"account_id": payable.id, "debit": 0.0,   "credit": 800.0},
        ],
        source_type="rollup_audit_fixture",
        source_id=0,
    )
    db.session.commit()


def _teardown_company(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine,
    )
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── Balance sheet rollup ───────────────────────────────────────────────
@check("BS-1. 1100 (Current Assets header) rolls up 800 = 500 + 300")
def _():
    from app.services.reports import balance_sheet
    data = balance_sheet(_STATE["company_id"])
    by_code = {r["code"]: r["balance"] for r in data["assets"]}
    assert "1100" in by_code, f"parent 1100 not in assets: {sorted(by_code)}"
    assert abs(by_code["1100"] - 800.0) < 0.01, \
        f"1100 rolled {by_code['1100']} != 800"
    return f"1100 = {by_code['1100']:.2f}"


@check("BS-2. 1120 (Banks header) rolls up 300 from single child 1121")
def _():
    from app.services.reports import balance_sheet
    data = balance_sheet(_STATE["company_id"])
    by_code = {r["code"]: r["balance"] for r in data["assets"]}
    assert "1120" in by_code, "1120 missing"
    assert abs(by_code["1120"] - 300.0) < 0.01, \
        f"1120 rolled {by_code['1120']} != 300"
    return f"1120 = {by_code['1120']:.2f}"


@check("BS-3. Leaf balances unchanged (1110=500, 1121=300)")
def _():
    from app.services.reports import balance_sheet
    data = balance_sheet(_STATE["company_id"])
    by_code = {r["code"]: r["balance"] for r in data["assets"]}
    assert abs(by_code["1110"] - 500.0) < 0.01, \
        f"1110 leaf {by_code['1110']} != 500"
    assert abs(by_code["1121"] - 300.0) < 0.01, \
        f"1121 leaf {by_code['1121']} != 300"
    return "1110=500, 1121=300"


@check("BS-4. totals[\"assets\"] == 800 (own posts, no double-count)")
def _():
    from app.services.reports import balance_sheet
    data = balance_sheet(_STATE["company_id"])
    got = data["totals"]["assets"]
    assert abs(got - 800.0) < 0.01, \
        f"totals[assets] = {got} != 800 — parent/child double-counted"
    return f"totals[assets] = {got:.2f}"


@check("BS-5. Balance sheet still balanced (assets == liab + equity)")
def _():
    from app.services.reports import balance_sheet
    data = balance_sheet(_STATE["company_id"])
    assert data["balanced"], \
        (f"unbalanced: assets={data['totals']['assets']} "
         f"liab+eq={data['total_liab_equity']}")
    return f"assets={data['totals']['assets']:.2f} L+E={data['total_liab_equity']:.2f}"


# ─── Trial balance rollup ───────────────────────────────────────────────
@check("TB-1. 1100 debit column rolls up 800; credit 0")
def _():
    from app.services.reports import trial_balance_report
    data = trial_balance_report(_STATE["company_id"])
    by_code = {r["code"]: r for r in data["rows"]}
    assert "1100" in by_code, "1100 missing in trial balance"
    r = by_code["1100"]
    assert abs(r["debit"] - 800.0) < 0.01, \
        f"1100 debit rolled {r['debit']} != 800"
    assert abs(r["credit"] - 0.0) < 0.01, \
        f"1100 credit rolled {r['credit']} != 0"
    return f"1100 dr={r['debit']:.2f} cr={r['credit']:.2f}"


@check("TB-2. 1120 debit column rolls up 300")
def _():
    from app.services.reports import trial_balance_report
    data = trial_balance_report(_STATE["company_id"])
    by_code = {r["code"]: r for r in data["rows"]}
    r = by_code["1120"]
    assert abs(r["debit"] - 300.0) < 0.01, \
        f"1120 debit rolled {r['debit']} != 300"
    return f"1120 dr={r['debit']:.2f}"


@check("TB-3. Leaf 1110 debit still 500 (rollup no-op on leaves)")
def _():
    from app.services.reports import trial_balance_report
    data = trial_balance_report(_STATE["company_id"])
    by_code = {r["code"]: r for r in data["rows"]}
    r = by_code["1110"]
    assert abs(r["debit"] - 500.0) < 0.01, \
        f"1110 debit {r['debit']} != 500"
    return f"1110 dr={r['debit']:.2f}"


@check("TB-4. totals[\"debit\"] == totals[\"credit\"] == 800 (Σ own only)")
def _():
    from app.services.reports import trial_balance_report
    data = trial_balance_report(_STATE["company_id"])
    d = data["totals"]["debit"]
    c = data["totals"]["credit"]
    assert abs(d - 800.0) < 0.01, \
        f"totals[debit] = {d} != 800 — parent + leaves double-counted"
    assert abs(c - 800.0) < 0.01, \
        f"totals[credit] = {c} != 800"
    assert data["totals"]["balanced"], \
        f"trial balance not balanced: diff={data['totals']['diff']}"
    return f"Σd={d:.2f} Σc={c:.2f}"


# ─── Run ────────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
