#!/usr/bin/env python3
"""MARSOUD-ATTENDANCE-RANGE (Abdelhamid 2026-08-01).

Batch 9 Ticket 5. Attendance exceptions used to be entered one
day at a time. Adds an optional date_to that bulk-creates one
AttendanceException per non-rest day in the range, skipping
days that already have one.

Checks:
  1. Single-day (date only, no date_to) → 1 row created.
  2. 5-day range, all business days → 5 rows created.
  3. Range including weekly rest days → those days skipped.
  4. date_to < date → refused with LeaveError, no rows.
  5. Range where 1 day already has an exception → other days
     created, existing one preserved (no crash, no dupe).
  6. Range > 90 days → refused with cap error.
  7. LATE type over a range → refused (per-day only makes sense).
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__AR_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})


def _bootstrap(suffix, rest="4,5"):
    """Company + one employee, with weekend_days = rest CSV."""
    from app.models import Company
    from app.models.payroll import Employee
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__AR_{suffix}__", base_currency="EGP",
                 subdomain=f"ar-{suffix.lower()}",
                 weekend_days=rest,
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    emp = Employee(company_id=c.id, name="test-emp",
                    basic_salary=1000, allowances=0)
    db.session.add(emp); db.session.commit()
    return c, emp


def _count_exceptions(emp_id):
    from app.models import AttendanceException
    return AttendanceException.query.filter_by(
        employee_id=emp_id).count()


@check("1. Single-day (date only) → 1 row")
def _():
    from app.services.leave import create_exception_range
    _teardown()
    c, emp = _bootstrap("A")
    summary = create_exception_range(
        company_id=c.id, employee_id=emp.id,
        date_from=date(2026, 3, 2),  # a Monday
        date_to=None, type_="ABSENT",
        rest_weekdays=c.rest_weekdays)
    assert summary["created"] == 1
    assert _count_exceptions(emp.id) == 1
    return "1 day → 1 exception"


@check("2. 5-day range, all business days → 5 rows")
def _():
    from app.services.leave import create_exception_range
    _teardown()
    c, emp = _bootstrap("B")
    # 2026-08-02 (Sun) → 2026-08-06 (Thu) — 5 business days
    # in the Gulf calendar (weekend = Fri/Sat = weekday 4,5).
    summary = create_exception_range(
        company_id=c.id, employee_id=emp.id,
        date_from=date(2026, 8, 2),
        date_to=date(2026, 8, 6),
        type_="ABSENT",
        rest_weekdays=c.rest_weekdays)
    assert summary["created"] == 5, \
        f"expected 5, got {summary['created']}"
    assert summary["skipped_weekend"] == 0
    return f"5 rows created"


@check("3. Range including weekly rest days → those skipped")
def _():
    from app.services.leave import create_exception_range
    _teardown()
    c, emp = _bootstrap("C")
    # 2026-08-07 (Fri) + 2026-08-08 (Sat) fall inside this
    # 10-day range. Both are default rest days → skipped.
    summary = create_exception_range(
        company_id=c.id, employee_id=emp.id,
        date_from=date(2026, 8, 3),   # Mon
        date_to=date(2026, 8, 12),    # Wed (10 days total)
        type_="ABSENT",
        rest_weekdays=c.rest_weekdays)
    assert summary["skipped_weekend"] == 2, \
        f"weekend skips={summary['skipped_weekend']}, want 2"
    assert summary["created"] == 8, \
        f"created={summary['created']}, want 8"
    return f"skipped {summary['skipped_weekend']} rest days"


@check("4. date_to < date → refused, no rows")
def _():
    from app.services.leave import create_exception_range, LeaveError
    _teardown()
    c, emp = _bootstrap("D")
    try:
        create_exception_range(
            company_id=c.id, employee_id=emp.id,
            date_from=date(2026, 8, 10),
            date_to=date(2026, 8, 5),  # earlier
            type_="ABSENT",
            rest_weekdays=c.rest_weekdays)
        assert False, "should have raised LeaveError"
    except LeaveError:
        pass
    assert _count_exceptions(emp.id) == 0
    return "backwards range refused"


@check("5. Range with 1 pre-existing day → others created, existing preserved")
def _():
    from app.services.leave import (
        create_exception, create_exception_range,
    )
    _teardown()
    c, emp = _bootstrap("E")
    # Pre-seed one exception on Aug 4 with a distinct note.
    create_exception(company_id=c.id, employee_id=emp.id,
                      date_=date(2026, 8, 4), type_="ABSENT",
                      note="pre-existing")
    # Now bulk-range Aug 3 → Aug 6 (4 business days: Mon-Thu).
    summary = create_exception_range(
        company_id=c.id, employee_id=emp.id,
        date_from=date(2026, 8, 3),
        date_to=date(2026, 8, 6),
        type_="ABSENT", note="bulk",
        rest_weekdays=c.rest_weekdays)
    assert summary["skipped_existing"] == 1
    assert summary["created"] == 3, \
        f"created={summary['created']}, want 3"
    # Pre-existing note preserved.
    from app.models import AttendanceException
    ex = AttendanceException.query.filter_by(
        employee_id=emp.id, date=date(2026, 8, 4)).first()
    assert ex.note == "pre-existing", \
        f"pre-existing note overwritten: {ex.note}"
    return "existing skipped, others created, note preserved"


@check("6. Range > 90 days → refused")
def _():
    from app.services.leave import create_exception_range, LeaveError
    _teardown()
    c, emp = _bootstrap("F")
    try:
        create_exception_range(
            company_id=c.id, employee_id=emp.id,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 12, 31),  # > 90 days
            type_="ABSENT",
            rest_weekdays=c.rest_weekdays)
        assert False, "should have raised"
    except LeaveError as e:
        assert "90" in str(e), f"cap message unclear: {e}"
    assert _count_exceptions(emp.id) == 0
    return "over-cap range refused"


@check("7. LATE type over a range → refused")
def _():
    from app.services.leave import create_exception_range, LeaveError
    _teardown()
    c, emp = _bootstrap("G")
    try:
        create_exception_range(
            company_id=c.id, employee_id=emp.id,
            date_from=date(2026, 8, 3),
            date_to=date(2026, 8, 6),
            type_="LATE",
            rest_weekdays=c.rest_weekdays)
        assert False, "should have raised"
    except LeaveError:
        pass
    assert _count_exceptions(emp.id) == 0
    return "LATE range refused"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
