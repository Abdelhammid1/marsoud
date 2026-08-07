#!/usr/bin/env python3
"""MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — audit for the fixed-asset
disposal flow.

Eight checks covering every acceptance criterion + the two guardrails
(depreciation-still-skips + /journals-reverse-refuses). Mirrors the
fixture shape from tests/audit_advance_installments.py.

Prefix `__DISPOSE_`; teardown sweeps by company_id + wipes orphan
journal_lines to survive SQLite's id-reuse (same trap the cash-
custody audit hit)."""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__DISPOSE_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Account,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__dispose__").first()
    if not plan:
        plan = Plan(code="__dispose__", name="D", name_ar="D",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "reports"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="dispose",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="dispose owner",
             is_active=True, status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"))
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    _STATE.update(company_id=c.id, user_id=u.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Orphan sweep — same SQLite id-reuse trap as cash-custody.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__DISPOSE_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__DISPOSE_%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__dispose__'"))


def _make_asset(*, cost, useful_life_years=5, acc_dep=0,
                 code="1210"):
    """Build a FixedAsset directly (no purchase journal — we
    focus on the disposal side). Sets accumulated_depreciation
    to `acc_dep` so we can test all three NBV scenarios cleanly."""
    from app.models import FixedAsset, Account
    acc = Account.query.filter_by(
        company_id=_STATE["company_id"], code=code).first()
    assert acc, f"seed missing account {code}"
    a = FixedAsset(
        company_id=_STATE["company_id"],
        name=f"asset-{cost}",
        purchase_date=date.today() - timedelta(days=365),
        cost=cost, useful_life_years=useful_life_years,
        accumulated_depreciation=acc_dep,
        account_id=acc.id,
    )
    db.session.add(a); db.session.commit()
    return a


# ─── Checks ────────────────────────────────────────────────────
@check("1. Fully-depreciated (NBV=0), no proceeds — Dr 1290 / Cr cost")
def _():
    from app.services.assets import dispose_asset
    from app.models import (
        FixedAsset, JournalLine, JournalEntry,
    )
    _setup()
    a = _make_asset(cost=10000, acc_dep=10000)  # NBV = 0
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="END_OF_LIFE",
                   created_by=_STATE["user_id"])
    db.session.refresh(a)
    assert a.is_disposed
    lines = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id).all()
    # Should be exactly 2 lines: Dr 1290 10000 / Cr 1210 10000.
    assert len(lines) == 2, f"expected 2 lines, got {len(lines)}"
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - 10000) < 0.01 and abs(total_cr - 10000) < 0.01
    return f"NBV=0 → 2 lines, balanced at 10000"


@check("2. NBV=5000, proceeds=3000 → loss on 5950")
def _():
    from app.services.assets import dispose_asset
    from app.models import Account, JournalLine
    _setup()
    a = _make_asset(cost=10000, acc_dep=5000)  # NBV = 5000
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="SOLD", proceeds=3000,
                   funding="cash",
                   created_by=_STATE["user_id"])
    db.session.refresh(a)
    # Loss line hits 5950.
    acc5950 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5950").first()
    loss = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id,
        account_id=acc5950.id).all()
    assert loss and abs(float(loss[0].debit or 0) - 2000) < 0.01, (
        f"expected 2000 loss on 5950, got {loss}")
    # Balance check.
    all_lines = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id).all()
    tot_dr = sum(float(l.debit or 0) for l in all_lines)
    tot_cr = sum(float(l.credit or 0) for l in all_lines)
    assert abs(tot_dr - 10000) < 0.01 and abs(tot_cr - 10000) < 0.01
    return f"loss 2000 on 5950, journal balanced at 10000"


@check("3. NBV=2000, proceeds=3000 → gain on 4550")
def _():
    from app.services.assets import dispose_asset
    from app.models import Account, JournalLine
    _setup()
    a = _make_asset(cost=10000, acc_dep=8000)  # NBV = 2000
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="SOLD", proceeds=3000,
                   created_by=_STATE["user_id"])
    db.session.refresh(a)
    acc4550 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="4550").first()
    gain = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id,
        account_id=acc4550.id).all()
    assert gain and abs(float(gain[0].credit or 0) - 1000) < 0.01, (
        f"expected 1000 gain on 4550, got {gain}")
    return f"gain 1000 on 4550"


@check("4. charged_account_id overrides 5950 (item-custody seam)")
def _():
    from app.services.assets import dispose_asset
    from app.models import Account, JournalLine
    _setup()
    # Use 5299 (Miscellaneous Expenses) as the mock charged account
    # — anything postable + company-scoped works. In item-custody's
    # actual use this is the employee's 2130 sub-account.
    charged = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5299").first()
    a = _make_asset(cost=10000, acc_dep=5000)  # NBV = 5000, loss=2000
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="LOST", proceeds=3000,
                   charged_account_id=charged.id,
                   created_by=_STATE["user_id"])
    db.session.refresh(a)
    # Loss did NOT hit 5950.
    acc5950 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5950").first()
    on_5950 = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id,
        account_id=acc5950.id).all()
    assert not on_5950, (
        f"loss leaked to 5950 despite charged_account_id: {on_5950}")
    # Loss DID hit the charged account.
    on_charged = JournalLine.query.filter_by(
        entry_id=a.disposal_journal_entry_id,
        account_id=charged.id).all()
    assert on_charged and abs(float(on_charged[0].debit or 0) - 2000) < 0.01
    return f"loss 2000 routed to 5299 (mock item-custody target)"


@check("5. Double-dispose refused with named earlier date")
def _():
    from app.services.assets import dispose_asset, AssetError
    _setup()
    a = _make_asset(cost=5000, acc_dep=5000)
    dispose_asset(a.id, disposal_date=date(2026, 5, 1),
                   reason="END_OF_LIFE",
                   created_by=_STATE["user_id"])
    try:
        dispose_asset(a.id, disposal_date=date.today(),
                       reason="SOLD",
                       created_by=_STATE["user_id"])
    except AssetError as e:
        assert "مشطوب بالفعل" in str(e)
        assert "2026-05-01" in str(e), f"date missing: {e}"
        return f"refused + names original date"
    raise AssertionError("double-dispose accepted")


@check("6. post_monthly_depreciation skips a disposed asset")
def _():
    from app.services.assets import (
        dispose_asset, post_monthly_depreciation,
    )
    _setup()
    a = _make_asset(cost=12000, useful_life_years=1, acc_dep=0)
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="OTHER", created_by=_STATE["user_id"])
    # Now try to depreciate this month.
    result = post_monthly_depreciation(
        _STATE["company_id"],
        date.today().year, date.today().month,
        created_by=_STATE["user_id"])
    assert a.name not in [n for n, _ in result["processed"]], (
        f"disposed asset was processed: {result['processed']}")
    # It's not in `skipped` either — the depreciation query never
    # even loaded it (is_disposed=False filter at line 74). That's
    # the guarantee the ticket asks for.
    return f"disposed asset invisible to depreciation run"


@check("7. reverse_journal on disposal entry raises LedgerError")
def _():
    from app.services.assets import dispose_asset
    from app.services.ledger import reverse_journal, LedgerError
    _setup()
    a = _make_asset(cost=8000, acc_dep=3000)
    dispose_asset(a.id, disposal_date=date.today(),
                   reason="SOLD", proceeds=5000,
                   created_by=_STATE["user_id"])
    try:
        reverse_journal(a.disposal_journal_entry_id,
                         created_by=_STATE["user_id"])
    except LedgerError as e:
        assert "شطب" in str(e) and "تصحيح" in str(e)
        db.session.refresh(a)
        assert a.is_disposed is True, (
            "is_disposed changed despite refusal")
        return f"reversal refused; flag intact"
    raise AssertionError("disposal reversal accepted")


@check("8. fixed_assets_report splits active + disposed")
def _():
    from app.services.assets import dispose_asset
    from app.services.reports import fixed_assets_report
    _setup()
    live = _make_asset(cost=1000, acc_dep=0)     # stays live
    d1 = _make_asset(cost=2000, acc_dep=2000)    # dispose
    d2 = _make_asset(cost=5000, acc_dep=1000)    # dispose w/ proceeds
    dispose_asset(d1.id, disposal_date=date.today(),
                   reason="END_OF_LIFE",
                   created_by=_STATE["user_id"])
    dispose_asset(d2.id, disposal_date=date.today(),
                   reason="SOLD", proceeds=3500,
                   created_by=_STATE["user_id"])
    r = fixed_assets_report(_STATE["company_id"])
    assert len(r["active"]["rows"]) == 1, (
        f"expected 1 active, got {len(r['active']['rows'])}")
    assert len(r["disposed"]["rows"]) == 2, (
        f"expected 2 disposed, got {len(r['disposed']['rows'])}")
    # Back-compat: top-level rows == active rows.
    assert r["rows"] == r["active"]["rows"]
    # Disposed totals carry a proceeds sum the active side doesn't.
    assert abs(r["disposed"]["totals"]["proceeds"] - 3500) < 0.01
    return f"active={len(r['active']['rows'])} · disposed={len(r['disposed']['rows'])} · proceeds=3500"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}\n        => {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
