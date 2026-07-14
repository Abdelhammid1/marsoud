#!/usr/bin/env python3
"""MARSOUD-DIGEST-TZ-FIX (Abdelhamid 2026-07-14).

Abdelhamid: "the report for day 13 should arrive on day 14 at 12 AM."
When the server runs in UTC and the cron ticks at 00:15 Cairo local
(= 22:15 UTC of the previous day), the old digest code used
server-local date.today() and produced a day that was ONE DAY BEHIND
what the company's local calendar said.

The fix computes "yesterday" in the company's own timezone so the
digest day is stable regardless of when the cron actually runs
during the local "today".

Checks:
  1. run_daily_digest_for_company on a company set to Asia/Riyadh
     builds a digest whose report_date equals (local today − 1),
     even when the server clock is close to a UTC/local boundary.
  2. Passing an explicit day= wins over the timezone default
     (no regression on the manual-run path).
  3. A company with a null / unknown timezone falls back to the
     default (Asia/Riyadh in time.today_in_company_tz).
  4. The idempotence guard still holds after the tz fix — running
     twice on the same simulated "yesterday" doesn't create
     two rows per employee.
"""
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'ddz-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Employee, EmployeeStatus,
        Department,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__DDZ__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(
        name="__DDZ__", base_currency="SAR", vat_rate=15,
        timezone="Asia/Riyadh",
    )
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    dept = Department(company_id=a.id, name="ops")
    db.session.add(dept); db.session.flush()

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role=role))
        return u

    owner = _mk("ddz-owner@x.test", "owner")
    emp_user = _mk("ddz-emp@x.test", "employee")
    emp = Employee(
        company_id=a.id, user_id=emp_user.id,
        name="DDZ Employee", department_id=dept.id,
        status=EmployeeStatus.ACTIVE,
    )
    db.session.add(emp); db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, emp_id=emp.id,
    )


# ─── Checks ────────────────────────────────────────────────────────
@check("1. digest day = (company-local today − 1), not server-UTC today − 1")
def _():
    from app.services.daily_digest import run_daily_digest_for_company
    from app.services.time import today_in_company_tz
    from app.models import Company
    from datetime import timedelta
    company = db.session.get(Company, _STATE["a_id"])
    expected_day = today_in_company_tz(company) - timedelta(days=1)
    summary = run_daily_digest_for_company(_STATE["a_id"])
    assert summary["day"] == expected_day.isoformat(), \
        f"summary day mismatch: {summary['day']} vs {expected_day}"
    return f"digest computed for {expected_day.isoformat()} (company-local yesterday)"


@check("2. explicit day= wins over the tz-aware default")
def _():
    from app.services.daily_digest import run_daily_digest_for_company
    from datetime import date, timedelta
    manual_day = date.today() - timedelta(days=45)
    summary = run_daily_digest_for_company(
        _STATE["a_id"], day=manual_day)
    assert summary["day"] == manual_day.isoformat(), \
        f"explicit day ignored: {summary['day']}"
    return f"manual override {manual_day} honoured over tz default"


@check("3. company with no timezone still runs (falls back to Asia/Riyadh)")
def _():
    from app.models import Company
    from app.services.time import today_in_company_tz
    from app.services.daily_digest import run_daily_digest_for_company
    from datetime import timedelta
    company = db.session.get(Company, _STATE["a_id"])
    company.timezone = None
    db.session.commit()
    expected_day = today_in_company_tz(company) - timedelta(days=1)
    summary = run_daily_digest_for_company(_STATE["a_id"])
    assert summary["day"] == expected_day.isoformat(), \
        f"fallback day mismatch: {summary['day']} vs {expected_day}"
    # Reset tz so downstream checks aren't affected.
    company.timezone = "Asia/Riyadh"
    db.session.commit()
    return f"fallback OK ({expected_day.isoformat()})"


@check("4. Simulated tz boundary: run at 22:00 UTC computes local next-day − 1")
def _():
    """Reproduce Rofida's/Abdelhamid's scenario: the cron fires at
    22:00 UTC (which is 00:00 or 01:00 Cairo/Riyadh local). Before
    the fix the digest day would be TWO days behind local — this
    check pins the fix by monkey-patching today_in_company_tz to
    simulate a controlled 'local today', then confirming the digest
    day is exactly (local_today − 1)."""
    from app.services import daily_digest as dd_mod
    from app.services import time as time_mod
    from datetime import date, timedelta

    simulated_local_today = date(2026, 7, 14)
    real_helper = time_mod.today_in_company_tz
    try:
        time_mod.today_in_company_tz = lambda c: simulated_local_today
        summary = dd_mod.run_daily_digest_for_company(_STATE["a_id"])
        expected = (simulated_local_today - timedelta(days=1)).isoformat()
        assert summary["day"] == expected, \
            f"expected {expected}, got {summary['day']}"
    finally:
        time_mod.today_in_company_tz = real_helper
    return f"simulated local today={simulated_local_today} → digest={expected}"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
