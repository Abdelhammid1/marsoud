#!/usr/bin/env python3
"""MARSOUD-EVAL-METRIC-LOG — audit for the raw-log tier that sits
under EmployeeMetricActual. Khadeeja records datapoints as they
happen; the aggregator collapses them into `actual_value` at
cycle-close time using the target's aggregation_method.

Coverage:
  1. Model round-trip: MetricLogEntry inserts and reads back.
  2. log_metric_entry rejects a metric_key with no matching target
     (dependent-dropdown enforced at the service layer too).
  3. log_metric_entry refused when the cycle is LOCKED.
  4. SUM aggregation: three entries with values 3, 5, 7 → actual = 15.
  5. AVERAGE aggregation: same three values → actual = 5.
  6. LATEST aggregation: takes the most-recent entry_date, not the
     largest value.
  7. aggregate_actuals_for_cycle is idempotent — re-runs overwrite
     the same actual row with the same numbers.
  8. Empty log set → target's actual is untouched (any manual value
     survives, so mixed workflows work).
  9. Transitioning OPEN → SUBMITTED auto-fires the aggregator.
 10. Dependent-dropdown API returns only the targets for
     (cycle, employee), cross-tenant safe (empty JSON on mismatch).
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

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


def _setup():
    from app.models import (
        Company, User, user_companies, Employee, EmployeeStatus,
    )
    from werkzeug.security import generate_password_hash
    for name in ("__EVAL_LOG_AUDIT__", "__EVAL_LOG_AUDIT_B__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__EVAL_LOG_AUDIT__", base_currency="SAR")
    b = Company(name="__EVAL_LOG_AUDIT_B__", base_currency="SAR")
    db.session.add_all([a, b]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(b.id)

    def _mk_user(email, company_id):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=company_id, role="owner"))
        return u

    owner_a = _mk_user("eval-log-a@x.test", a.id)
    owner_b = _mk_user("eval-log-b@x.test", b.id)
    emp_a = Employee(company_id=a.id, name="فوزي",
                       status=EmployeeStatus.ACTIVE)
    emp_b = Employee(company_id=b.id, name="ناجي",
                       status=EmployeeStatus.ACTIVE)
    db.session.add_all([emp_a, emp_b])
    db.session.commit()
    _STATE.update(
        company_a_id=a.id, company_b_id=b.id,
        owner_a_id=owner_a.id, owner_b_id=owner_b.id,
        emp_a_id=emp_a.id, emp_b_id=emp_b.id,
    )


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # employee_targets / employee_metric_actuals /
        # employee_evaluations don't have their own company_id column
        # (they scope by cycle_id → cycle.company_id). Sweep them
        # via that FK BEFORE we delete the cycles, otherwise the
        # rows survive and pollute the next audit run.
        cycles_sql = (
            "SELECT id FROM evaluation_cycles WHERE company_id = :c"
        )
        for tbl in ("employee_evaluations", "employee_metric_actuals",
                     "employee_targets", "metric_log_entries"):
            try:
                conn.execute(text(
                    f"DELETE FROM {tbl} WHERE cycle_id IN ({cycles_sql})"
                ), {"c": company_id})
            except Exception:
                pass
        # Now safe to drop the parent rows.
        try:
            conn.execute(text(
                "DELETE FROM evaluation_cycles WHERE company_id = :c"),
                {"c": company_id})
        except Exception:
            pass
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'eval-log-%@x.test'"))


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _fresh_cycle(company_id, name="log"):
    from app.services.evaluation import create_cycle
    return create_cycle(
        company_id=company_id, name=name, period_type="MONTHLY",
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        created_by_id=_STATE["owner_a_id"],
    )


def _seed_target(cycle, employee_id, metric_key, method="SUM"):
    from app.services.evaluation import upsert_target
    return upsert_target(
        cycle=cycle, employee_id=employee_id,
        metric_key=metric_key, target_value=100, weight_pct=100,
        category="TARGET_ACHIEVEMENT",
        aggregation_method=method,
    )


# ─── Model + service checks ───────────────────────────────────────────
@check("1. MetricLogEntry round-trips through the DB")
def _():
    from app.models import MetricLogEntry
    c = _fresh_cycle(_STATE["company_a_id"], "rt")
    _seed_target(c, _STATE["emp_a_id"], "x")
    from app.services.evaluation import log_metric_entry
    e = log_metric_entry(
        company_id=_STATE["company_a_id"], cycle=c,
        employee_id=_STATE["emp_a_id"], metric_key="x",
        entry_date=date(2026, 7, 15), value=5,
        entered_by_id=_STATE["owner_a_id"],
    )
    reread = db.session.get(MetricLogEntry, e.id)
    assert reread and float(reread.value) == 5.0
    assert reread.entry_date == date(2026, 7, 15)
    return f"row {e.id} round-tripped"


@check("2. log_metric_entry refuses a metric_key with no matching target")
def _():
    from app.services.evaluation import log_metric_entry, EvaluationError
    c = _fresh_cycle(_STATE["company_a_id"], "orphan")
    raised = False
    try:
        log_metric_entry(
            company_id=_STATE["company_a_id"], cycle=c,
            employee_id=_STATE["emp_a_id"],
            metric_key="orphan_metric",
            entry_date=date(2026, 7, 15), value=1,
            entered_by_id=_STATE["owner_a_id"],
        )
    except EvaluationError:
        raised = True
    assert raised
    return "orphan metric refused"


@check("3. log_metric_entry refused when cycle is LOCKED")
def _():
    from app.services.evaluation import (
        log_metric_entry, transition_status, EvaluationError,
    )
    c = _fresh_cycle(_STATE["company_a_id"], "locked")
    _seed_target(c, _STATE["emp_a_id"], "x")
    # OPEN → SUBMITTED → LOCKED
    transition_status(c, "SUBMITTED")
    transition_status(c, "LOCKED")
    raised = False
    try:
        log_metric_entry(
            company_id=_STATE["company_a_id"], cycle=c,
            employee_id=_STATE["emp_a_id"], metric_key="x",
            entry_date=date(2026, 7, 15), value=1,
            entered_by_id=_STATE["owner_a_id"],
        )
    except EvaluationError:
        raised = True
    assert raised
    return "LOCKED cycle refuses new logs"


@check("4. SUM aggregation collapses 3+5+7 → actual=15")
def _():
    from app.services.evaluation import (
        log_metric_entry, aggregate_actuals_for_cycle,
    )
    from app.models import EmployeeMetricActual, ActualSource
    c = _fresh_cycle(_STATE["company_a_id"], "sum")
    _seed_target(c, _STATE["emp_a_id"], "sales", method="SUM")
    for d, v in [(1, 3), (2, 5), (3, 7)]:
        log_metric_entry(
            company_id=_STATE["company_a_id"], cycle=c,
            employee_id=_STATE["emp_a_id"], metric_key="sales",
            entry_date=date(2026, 7, d), value=v,
            entered_by_id=_STATE["owner_a_id"],
        )
    aggregate_actuals_for_cycle(c)
    row = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"],
        metric_key="sales",
    ).first()
    assert row is not None
    assert float(row.actual_value) == 15.0, f"actual={row.actual_value}"
    assert row.source == ActualSource.AUTO_AGGREGATED.value
    return f"actual=15 (3+5+7)"


@check("5. AVERAGE aggregation collapses 3+5+7 → actual=5")
def _():
    from app.services.evaluation import (
        log_metric_entry, aggregate_actuals_for_cycle,
    )
    from app.models import EmployeeMetricActual
    c = _fresh_cycle(_STATE["company_a_id"], "avg")
    _seed_target(c, _STATE["emp_a_id"], "leads", method="AVERAGE")
    for d, v in [(1, 3), (2, 5), (3, 7)]:
        log_metric_entry(
            company_id=_STATE["company_a_id"], cycle=c,
            employee_id=_STATE["emp_a_id"], metric_key="leads",
            entry_date=date(2026, 7, d), value=v,
            entered_by_id=_STATE["owner_a_id"],
        )
    aggregate_actuals_for_cycle(c)
    row = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"],
        metric_key="leads",
    ).first()
    assert abs(float(row.actual_value) - 5.0) < 0.01
    return f"actual=5.0 (mean of 3,5,7)"


@check("6. LATEST aggregation picks the most-recent entry_date")
def _():
    from app.services.evaluation import (
        log_metric_entry, aggregate_actuals_for_cycle,
    )
    from app.models import EmployeeMetricActual
    c = _fresh_cycle(_STATE["company_a_id"], "latest")
    _seed_target(c, _STATE["emp_a_id"], "plan", method="LATEST")
    # Log values with mixed dates; largest date wins REGARDLESS of value.
    # value 99 on day 1, value 1 on day 15 → LATEST = 1.
    log_metric_entry(
        company_id=_STATE["company_a_id"], cycle=c,
        employee_id=_STATE["emp_a_id"], metric_key="plan",
        entry_date=date(2026, 7, 1), value=99,
        entered_by_id=_STATE["owner_a_id"],
    )
    log_metric_entry(
        company_id=_STATE["company_a_id"], cycle=c,
        employee_id=_STATE["emp_a_id"], metric_key="plan",
        entry_date=date(2026, 7, 15), value=1,
        entered_by_id=_STATE["owner_a_id"],
    )
    aggregate_actuals_for_cycle(c)
    row = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"],
        metric_key="plan",
    ).first()
    assert float(row.actual_value) == 1.0, \
        f"expected 1 (latest by date), got {row.actual_value}"
    return "LATEST = 1 (day 15 wins over day 1's 99)"


@check("7. aggregate is idempotent — same numbers on re-run")
def _():
    from app.services.evaluation import (
        log_metric_entry, aggregate_actuals_for_cycle,
    )
    from app.models import EmployeeMetricActual
    c = _fresh_cycle(_STATE["company_a_id"], "idem")
    _seed_target(c, _STATE["emp_a_id"], "y", method="SUM")
    log_metric_entry(
        company_id=_STATE["company_a_id"], cycle=c,
        employee_id=_STATE["emp_a_id"], metric_key="y",
        entry_date=date(2026, 7, 5), value=10,
        entered_by_id=_STATE["owner_a_id"],
    )
    aggregate_actuals_for_cycle(c)
    aggregate_actuals_for_cycle(c)
    aggregate_actuals_for_cycle(c)
    rows = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"], metric_key="y",
    ).all()
    assert len(rows) == 1
    assert float(rows[0].actual_value) == 10.0
    return "3× runs → 1 row, value stable"


@check("8. empty log set — target's actual stays untouched (mixed workflow)")
def _():
    """If Khadeeja has a manual EmployeeMetricActual already
    entered for a target that has no logs, the aggregator MUST
    NOT overwrite it with zero. Verifies the "no logs → skip"
    branch of aggregate_actuals_for_cycle."""
    from app.services.evaluation import (
        upsert_actual, aggregate_actuals_for_cycle,
    )
    from app.models import EmployeeMetricActual
    c = _fresh_cycle(_STATE["company_a_id"], "mixed")
    _seed_target(c, _STATE["emp_a_id"], "manual_metric", method="SUM")
    # Manual actual — no logs at all for this metric.
    upsert_actual(
        cycle=c, employee_id=_STATE["emp_a_id"],
        metric_key="manual_metric", actual_value=42,
        source="MANUAL",
    )
    aggregate_actuals_for_cycle(c)
    row = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"],
        metric_key="manual_metric",
    ).first()
    assert float(row.actual_value) == 42.0, \
        f"manual value was clobbered: {row.actual_value}"
    assert row.source == "MANUAL"
    return "manual actual preserved (42, source=MANUAL)"


@check("9. OPEN → SUBMITTED auto-fires aggregate_actuals_for_cycle")
def _():
    from app.services.evaluation import (
        log_metric_entry, transition_status,
    )
    from app.models import EmployeeMetricActual, ActualSource
    c = _fresh_cycle(_STATE["company_a_id"], "auto")
    _seed_target(c, _STATE["emp_a_id"], "auto_metric", method="SUM")
    log_metric_entry(
        company_id=_STATE["company_a_id"], cycle=c,
        employee_id=_STATE["emp_a_id"], metric_key="auto_metric",
        entry_date=date(2026, 7, 20), value=8,
        entered_by_id=_STATE["owner_a_id"],
    )
    # No manual aggregate call — the transition should fire it.
    transition_status(c, "SUBMITTED")
    row = EmployeeMetricActual.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"],
        metric_key="auto_metric",
    ).first()
    assert row is not None
    assert float(row.actual_value) == 8.0
    assert row.source == ActualSource.AUTO_AGGREGATED.value
    return "transition fired the aggregate + actual=8"


@check("10. dependent-dropdown API returns only agreed targets, cross-tenant safe")
def _():
    from flask import current_app
    c = _fresh_cycle(_STATE["company_a_id"], "api")
    _seed_target(c, _STATE["emp_a_id"], "leads")
    _seed_target(c, _STATE["emp_a_id"], "calls")

    # Owner in company A → gets the two targets
    _reset_g()
    client_a = current_app.test_client()
    with client_a.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_a_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_a_id"]
    r = client_a.get(
        f"/evaluations/api/targets"
        f"?cycle_id={c.id}&employee_id={_STATE['emp_a_id']}"
    )
    assert r.status_code == 200
    data = r.get_json()
    keys = {t["metric_key"] for t in data}
    assert keys == {"leads", "calls"}, f"got {keys}"

    # Owner in company B → gets an empty list for A's cycle_id
    _reset_g()
    client_b = current_app.test_client()
    with client_b.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_b_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_b_id"]
    r = client_b.get(
        f"/evaluations/api/targets"
        f"?cycle_id={c.id}&employee_id={_STATE['emp_a_id']}"
    )
    assert r.status_code == 200
    assert r.get_json() == [], "cross-tenant leak on API"
    return "two targets returned; empty for other tenant"


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
                for k in ("company_a_id", "company_b_id"):
                    if k in _STATE:
                        _teardown(_STATE[k])
                print("\n(cleaned up fixture companies)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
