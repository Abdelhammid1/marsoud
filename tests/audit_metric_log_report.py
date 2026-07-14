#!/usr/bin/env python3
"""MARSOUD-METRIC-LOG-REPORT (Abdelhamid 2026-07-14).

Abdelhamid: "we want to see what we logged in metric entries for each
employee, like reports." Existing /evaluations/logs/ was the last 30
raw entries. This adds a per-employee grouped report at
/reports/metric-logs.

Checks:
  1. collect_per_employee returns one row per employee with metric
     buckets aggregated.
  2. Aggregation math: sum, avg, min, max, latest are all correct.
  3. Employee filter narrows to one employee only.
  4. Cycle filter narrows to one cycle only.
  5. Metric filter narrows to one metric_key only.
  6. Date range filter respects entry_date bounds.
  7. Different cycles for the same (employee, metric_key) stay in
     separate buckets — not conflated.
  8. Ordering: employees alphabetical, metrics inside per (cycle, key).
  9. HTTP GET /reports/metric-logs renders + shows aggregated numbers.
 10. available_metric_keys returns distinct sorted keys.
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
            "DELETE FROM users WHERE email LIKE 'mlr-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Employee, EmployeeStatus,
        Department, EvaluationCycle, EvaluationCyclePeriod,
        EvaluationCycleStatus, MetricLogEntry,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__MLR__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__MLR__", base_currency="SAR", vat_rate=15,
                 timezone="Asia/Riyadh")
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

    owner = _mk("mlr-owner@x.test", "owner")
    emp1 = Employee(
        company_id=a.id, name="Rofida", department_id=dept.id,
        job_title="Analyst", status=EmployeeStatus.ACTIVE,
    )
    emp2 = Employee(
        company_id=a.id, name="Abdelhamid", department_id=dept.id,
        job_title="Owner", status=EmployeeStatus.ACTIVE,
    )
    db.session.add_all([emp1, emp2]); db.session.flush()

    # Two evaluation cycles — same employee, different cycles for
    # bucket separation check.
    cycle_a = EvaluationCycle(
        company_id=a.id, name="Cycle-A",
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        period_type=EvaluationCyclePeriod.MONTHLY.value,
        status=EvaluationCycleStatus.OPEN.value,
    )
    cycle_b = EvaluationCycle(
        company_id=a.id, name="Cycle-B",
        start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        period_type=EvaluationCyclePeriod.MONTHLY.value,
        status=EvaluationCycleStatus.OPEN.value,
    )
    db.session.add_all([cycle_a, cycle_b]); db.session.flush()

    # Log entries — mixed metrics + cycles + values.
    def _log(cyc, emp, key, day, value):
        e = MetricLogEntry(
            company_id=a.id, cycle_id=cyc.id, employee_id=emp.id,
            metric_key=key, entry_date=day, value=value,
            entered_by_id=owner.id,
        )
        db.session.add(e)
        return e

    # Rofida in Cycle-A: metric "calls" — sum 300, avg 100.
    _log(cycle_a, emp1, "calls", date(2026, 7, 10), 80)
    _log(cycle_a, emp1, "calls", date(2026, 7, 11), 100)
    _log(cycle_a, emp1, "calls", date(2026, 7, 15), 120)  # latest
    # Rofida in Cycle-A: metric "deals" — 1 entry.
    _log(cycle_a, emp1, "deals", date(2026, 7, 12), 5)
    # Rofida in Cycle-B: metric "calls" (same key, different cycle).
    _log(cycle_b, emp1, "calls", date(2026, 8, 5), 40)
    # Abdelhamid in Cycle-A: metric "hours" — 2 entries.
    _log(cycle_a, emp2, "hours", date(2026, 7, 3), 8)
    _log(cycle_a, emp2, "hours", date(2026, 7, 4), 9)
    db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id,
        emp1_id=emp1.id, emp2_id=emp2.id,
        cycle_a_id=cycle_a.id, cycle_b_id=cycle_b.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


# ─── Service checks ────────────────────────────────────────────────
@check("1. collect_per_employee returns rows per employee with metric buckets")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(_STATE["a_id"])
    assert len(rows) == 2, f"expected 2 employees, got {len(rows)}"
    names = sorted(r["employee"].name for r in rows)
    assert names == ["Abdelhamid", "Rofida"], f"names: {names}"
    return f"{len(rows)} employees"


@check("2. aggregation math: sum/avg/min/max/latest correct")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"], employee_id=_STATE["emp1_id"],
        cycle_id=_STATE["cycle_a_id"], metric_key="calls",
    )
    assert len(rows) == 1
    m = rows[0]["metrics"][0]
    assert m["count"] == 3, m
    assert m["sum"] == 300.0, m
    assert m["avg"] == 100.0, m
    assert m["min"] == 80.0, m
    assert m["max"] == 120.0, m
    assert m["latest_value"] == 120.0, m
    assert m["latest_date"] == date(2026, 7, 15), m
    return "sum=300 avg=100 min=80 max=120 latest=120 on 2026-07-15"


@check("3. employee filter narrows to one employee")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"], employee_id=_STATE["emp2_id"])
    assert len(rows) == 1
    assert rows[0]["employee"].name == "Abdelhamid"
    return "only Abdelhamid returned"


@check("4. cycle filter narrows to one cycle")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"], cycle_id=_STATE["cycle_b_id"])
    # Cycle-B only has Rofida/calls.
    assert len(rows) == 1
    assert rows[0]["employee"].name == "Rofida"
    metrics = rows[0]["metrics"]
    assert len(metrics) == 1
    assert metrics[0]["metric_key"] == "calls"
    assert metrics[0]["cycle_name"] == "Cycle-B"
    assert metrics[0]["sum"] == 40.0
    return "cycle B → Rofida/calls only"


@check("5. metric filter narrows to one metric_key")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"], metric_key="hours")
    assert len(rows) == 1
    assert rows[0]["employee"].name == "Abdelhamid"
    assert rows[0]["metrics"][0]["metric_key"] == "hours"
    return "hours → Abdelhamid only"


@check("6. date range filter respects entry_date")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"],
        date_from=date(2026, 7, 12), date_to=date(2026, 7, 20),
    )
    total = sum(r["total_entries"] for r in rows)
    # In window [7/12, 7/20]:
    #   · rofida/deals @ 7/12  → 1
    #   · rofida/calls @ 7/15  → 1
    # (7/10, 7/11 for calls excluded; Abdelhamid's 7/3, 7/4 excluded;
    # cycle-B 8/5 excluded.)
    assert total == 2, f"expected 2 entries in window, got {total}"
    return f"{total} entries in window"


@check("7. different cycles for same (employee, metric) stay separate buckets")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(
        _STATE["a_id"], employee_id=_STATE["emp1_id"],
        metric_key="calls",
    )
    assert len(rows) == 1
    metrics = rows[0]["metrics"]
    # Should have TWO buckets: one for Cycle-A, one for Cycle-B.
    assert len(metrics) == 2, f"expected 2 buckets, got {len(metrics)}"
    cycles = sorted(m["cycle_name"] for m in metrics)
    assert cycles == ["Cycle-A", "Cycle-B"], cycles
    return "Cycle-A/calls + Cycle-B/calls both present"


@check("8. ordering: employees alphabetical")
def _():
    from app.services.metric_log_report import collect_per_employee
    rows = collect_per_employee(_STATE["a_id"])
    names = [r["employee"].name for r in rows]
    assert names == sorted(names), f"not alphabetical: {names}"
    return f"order: {names}"


# ─── HTTP ──────────────────────────────────────────────────────────
@check("9. GET /reports/metric-logs renders with aggregated numbers")
def _():
    r = _login().get("/reports/metric-logs", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    # Employee names
    assert "Rofida" in body and "Abdelhamid" in body
    # Metric keys
    assert "calls" in body and "hours" in body
    # Cycles referenced
    assert "Cycle-A" in body and "Cycle-B" in body
    # Aggregated numbers — Rofida/calls Cycle-A sum=300, latest 120.
    assert "300" in body
    return "page shows names + metrics + aggregates"


@check("10. available_metric_keys returns distinct sorted list")
def _():
    from app.services.metric_log_report import available_metric_keys
    keys = available_metric_keys(_STATE["a_id"])
    # Distinct across all entries: calls, deals, hours.
    assert keys == sorted(set(keys)), "not sorted/deduped"
    assert set(keys) == {"calls", "deals", "hours"}
    return f"keys={keys}"


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
