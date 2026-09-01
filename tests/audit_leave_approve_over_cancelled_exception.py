#!/usr/bin/env python3
"""MARSOUD-TKT-LEAVE-APPROVE-CANCELLED-EXC (Abdelhamid 2026-08-31) —
approving a leave request must not 500 when a CANCELLED
AttendanceException already occupies the same (employee, date).

Before the fix, the approve path raw-INSERTed one exception per day
inside the range, and the DB-level `UniqueConstraint(employee_id,
date)` counted cancelled rows too — so an admin who had cancelled
today's ABSENT with "هعملها اجازة" and then approved the leave
request got a 500 (IntegrityError leaking out).

The fix upserts: if the day has a cancelled row, it's reactivated
as the new leave exception, preserving the cancel audit fields
(cancelled_by, cancelled_at, cancel_reason) so the trail survives.

Checks:
  1. The new `_upsert_leave_exception_for_day` helper exists in
     services.leave with the right shape.
  2. Empty day → helper INSERTs, returns "created".
  3. Cancelled row → helper REACTIVATES that row, preserving
     cancel audit, changing type/note/leave_request_id.
  4. Active (non-cancelled) row → helper raises LeaveError with
     the pre-fix conflict message (real conflict path unchanged).
  5. End-to-end: an approved leave over a day with a cancelled
     ABSENT succeeds and leaves ONE row on the day with the new
     leave type (no IntegrityError, no 500).
  6. End-to-end: the reactivation event lands in the activity log
     with old + new state + preserved cancel audit.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _boot_fixture(prefix):
    """Company + owner user + employee with a leave policy + a paid
    leave type. Returns (company_id, employee_id, owner_id,
    leave_type_id)."""
    from datetime import datetime, date
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import db
    from app.models import (
        Company, User, Plan, Employee, EmployeeStatus, LeaveType,
        LeaveBalance,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["hr"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    u = User(email=f"user__{prefix.lower()}__@x.io",
             full_name=f"User {prefix}",
             is_active=True, email_verified_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    emp = Employee(company_id=c.id, name="موظف الاختبار",
                   employee_number="EMP-TEST",
                   basic_salary=Decimal("14000"),
                   start_date=date(2026, 1, 1),
                   status=EmployeeStatus.ACTIVE)
    db.session.add(emp); db.session.commit()

    # A simple paid leave type + balance
    lt = LeaveType(company_id=c.id, name="سنوية",
                    is_paid=True,
                    accrual_per_month=Decimal("1.75"),
                    max_balance=Decimal("21"),
                    is_active=True)
    db.session.add(lt); db.session.commit()
    bal = LeaveBalance(employee_id=emp.id,
                       leave_type_id=lt.id, year=2026,
                       balance_days=Decimal("21"),
                       used_days=Decimal("0"))
    db.session.add(bal); db.session.commit()
    return c.id, emp.id, u.id, lt.id


def _teardown(prefix):
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        # leave_balances / attendance_exceptions / leave_requests
        # reference employees (which have company_id) — clean those
        # first via a join so the sorted-tables loop below doesn't
        # miss them (leave_balances has no company_id column).
        db.session.execute(text(
            "DELETE FROM leave_balances WHERE employee_id IN "
            "(SELECT id FROM employees WHERE company_id = :c)"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM attendance_exceptions WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM leave_requests WHERE company_id = :c"),
            {"c": cid})
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()


@check("1. _upsert_leave_exception_for_day helper exists with the right signature")
def _():
    from app.services import leave as _lv
    import inspect as _inspect
    fn = getattr(_lv, "_upsert_leave_exception_for_day", None)
    assert fn is not None, \
        "helper _upsert_leave_exception_for_day is missing"
    sig = _inspect.signature(fn)
    params = set(sig.parameters)
    expected = {"company_id", "employee_id", "date_", "type_", "note",
                "leave_request_id", "created_by"}
    assert expected <= params, \
        f"helper missing kwargs: {expected - params}"
    return "helper present with the expected 7 kwargs"


@check("2. empty day → INSERT (returns 'created')")
def _():
    from datetime import date
    from app import create_app, db
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.leave import _upsert_leave_exception_for_day

    app = create_app()
    with app.app_context():
        cid, eid, uid, ltid = _boot_fixture("LVC1")
        try:
            outcome = _upsert_leave_exception_for_day(
                company_id=cid, employee_id=eid,
                date_=date(2026, 8, 12),
                type_=AttendanceExceptionType.APPROVED_LEAVE,
                note="test",
                leave_request_id=None, created_by=uid,
            )
            db.session.commit()
            assert outcome == "created", f"got {outcome!r}"
            rows = (AttendanceException.query
                    .filter_by(employee_id=eid, date=date(2026, 8, 12))
                    .all())
            assert len(rows) == 1
            assert rows[0].type == AttendanceExceptionType.APPROVED_LEAVE
            assert rows[0].is_cancelled is False
            return "inserted one active row on an empty day"
        finally:
            _teardown("LVC1")


@check("3. cancelled row → REACTIVATE, preserving cancel audit")
def _():
    from datetime import date, datetime
    from app import create_app, db
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.leave import _upsert_leave_exception_for_day

    app = create_app()
    with app.app_context():
        cid, eid, uid, ltid = _boot_fixture("LVC2")
        try:
            # Seed a cancelled ABSENT row on the target date
            ex = AttendanceException(
                company_id=cid, employee_id=eid,
                date=date(2026, 8, 12),
                type=AttendanceExceptionType.ABSENT,
                note="لم يسجّل حضور",
                created_by=uid, is_cancelled=True,
                cancelled_by=uid, cancelled_at=datetime(2026, 8, 20, 10),
                cancel_reason="هعملها اجازة",
            )
            db.session.add(ex); db.session.commit()
            ex_id = ex.id

            outcome = _upsert_leave_exception_for_day(
                company_id=cid, employee_id=eid,
                date_=date(2026, 8, 12),
                type_=AttendanceExceptionType.APPROVED_LEAVE,
                note="إجازة سنوية",
                leave_request_id=999,  # fake — no FK enforcement in SQLite
                created_by=uid,
            )
            db.session.commit()
            assert outcome == "reactivated", f"got {outcome!r}"

            # SAME row (id unchanged) — repurposed, not duplicated
            fresh = db.session.get(AttendanceException, ex_id)
            assert fresh is not None
            assert fresh.type == AttendanceExceptionType.APPROVED_LEAVE
            assert fresh.is_cancelled is False
            assert fresh.note == "إجازة سنوية"
            assert fresh.leave_request_id == 999

            # Cancel audit preserved
            assert fresh.cancelled_by == uid, \
                "cancelled_by must survive the reactivation (audit trail)"
            assert fresh.cancelled_at == datetime(2026, 8, 20, 10), \
                "cancelled_at must survive"
            assert fresh.cancel_reason == "هعملها اجازة", \
                "cancel_reason must survive"

            # Only ONE row on that day (not two)
            all_rows = (AttendanceException.query
                        .filter_by(employee_id=eid, date=date(2026, 8, 12))
                        .all())
            assert len(all_rows) == 1, \
                f"expected 1 row after reactivation; got {len(all_rows)}"
            return "reactivated in-place with cancel audit intact"
        finally:
            _teardown("LVC2")


@check("4. active (non-cancelled) row → LeaveError with conflict message")
def _():
    from datetime import date
    from app import create_app, db
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.leave import (
        _upsert_leave_exception_for_day, LeaveError,
    )

    app = create_app()
    with app.app_context():
        cid, eid, uid, ltid = _boot_fixture("LVC3")
        try:
            db.session.add(AttendanceException(
                company_id=cid, employee_id=eid,
                date=date(2026, 8, 12),
                type=AttendanceExceptionType.LATE,
                duration_hours=1,
                is_cancelled=False, created_by=uid,
            ))
            db.session.commit()

            try:
                _upsert_leave_exception_for_day(
                    company_id=cid, employee_id=eid,
                    date_=date(2026, 8, 12),
                    type_=AttendanceExceptionType.APPROVED_LEAVE,
                    note="conflict",
                    leave_request_id=None, created_by=uid,
                )
            except LeaveError as e:
                assert "استثناء مسجل بالفعل" in str(e), \
                    f"error message drifted; got: {e}"
                return "active row correctly refused with the original message"
            raise AssertionError("expected LeaveError for active conflict")
        finally:
            _teardown("LVC3")


@check("5. end-to-end: approve_leave_request over cancelled ABSENT succeeds")
def _():
    from datetime import date, datetime
    from decimal import Decimal
    from app import create_app, db
    from app.models import (
        AttendanceException, AttendanceExceptionType,
        LeaveRequest, LeaveRequestStatus,
    )
    from app.services.leave import approve_leave_request

    app = create_app()
    with app.app_context():
        cid, eid, uid, ltid = _boot_fixture("LVC4")
        try:
            # 1. Simulate: employee had ABSENT logged, then cancelled
            ex = AttendanceException(
                company_id=cid, employee_id=eid,
                date=date(2026, 8, 12),
                type=AttendanceExceptionType.ABSENT,
                created_by=uid, is_cancelled=True,
                cancelled_by=uid, cancelled_at=datetime(2026, 8, 20, 10),
                cancel_reason="هعملها اجازة",
            )
            db.session.add(ex); db.session.commit()

            # 2. Submit a leave request covering the same day
            req = LeaveRequest(
                company_id=cid, employee_id=eid, leave_type_id=ltid,
                start_date=date(2026, 8, 12),
                end_date=date(2026, 8, 12),
                days_count=Decimal("1"),
                status=LeaveRequestStatus.PENDING,
                created_by=uid, reason="سفر عائلي",
            )
            db.session.add(req); db.session.commit()

            # 3. Approve — before the fix this raised IntegrityError
            _req, created = approve_leave_request(req, reviewer_id=uid)
            assert created == 1, f"expected 1 day approved; got {created}"
            assert _req.status == LeaveRequestStatus.APPROVED

            # 4. Exactly ONE row for the day + it's APPROVED_LEAVE
            rows = (AttendanceException.query
                    .filter_by(employee_id=eid, date=date(2026, 8, 12))
                    .all())
            assert len(rows) == 1, \
                f"expected 1 row after approve; got {len(rows)}"
            row = rows[0]
            assert row.type == AttendanceExceptionType.APPROVED_LEAVE, \
                f"row.type should be APPROVED_LEAVE; got {row.type}"
            assert row.is_cancelled is False, \
                "reactivated row must be active"
            assert row.leave_request_id == req.id, \
                "row must be linked to the approved leave request"
            # And the historical cancel audit is preserved
            assert row.cancel_reason == "هعملها اجازة", \
                "cancel_reason wiped — audit trail broken"
            return "end-to-end approve over cancelled ABSENT succeeded"
        finally:
            _teardown("LVC4")


@check("6. reactivation writes an activity_log UPDATE entry")
def _():
    from datetime import date, datetime
    from app import create_app, db
    from app.models import (
        AttendanceException, AttendanceExceptionType, UserActivityLog,
    )
    from app.services.leave import _upsert_leave_exception_for_day

    app = create_app()
    with app.app_context():
        cid, eid, uid, ltid = _boot_fixture("LVC5")
        try:
            ex = AttendanceException(
                company_id=cid, employee_id=eid,
                date=date(2026, 8, 20),
                type=AttendanceExceptionType.ABSENT,
                created_by=uid, is_cancelled=True,
                cancelled_by=uid, cancelled_at=datetime(2026, 8, 22, 10),
                cancel_reason="reactivation-test",
            )
            db.session.add(ex); db.session.commit()

            _upsert_leave_exception_for_day(
                company_id=cid, employee_id=eid,
                date_=date(2026, 8, 20),
                type_=AttendanceExceptionType.APPROVED_LEAVE,
                note="new leave", leave_request_id=None, created_by=uid,
            )
            db.session.commit()

            log = (UserActivityLog.query
                   .filter_by(entity_type="attendance_exception",
                              entity_id=ex.id)
                   .order_by(UserActivityLog.id.desc()).first())
            assert log is not None, \
                "reactivation should write an activity_log row"
            assert log.action_type == "UPDATE"
            # Check extra_data preserves the pre-cancel audit
            data = log.extra_data or {}
            if isinstance(data, str):
                import json
                data = json.loads(data)
            preserved = data.get("preserved_cancel_audit") or {}
            assert preserved.get("cancel_reason") == "reactivation-test", \
                f"preserved audit missing cancel_reason; got {preserved}"
            return "activity log written with old/new/preserved-cancel"
        finally:
            _teardown("LVC5")


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
