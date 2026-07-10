#!/usr/bin/env python3
"""MARSOUD-EVAL-CATEGORY-WEIGHT — audit for the per-(cycle, employee)
category weight override.

Coverage:
  1. get_category_weights returns the 60/25/15 defaults when no
     override exists for the (cycle, employee).
  2. set_category_weights persists the three-row set, one row per
     category.
  3. get_category_weights returns the persisted values after set.
  4. Sum-to-100 invariant is enforced (raises on 90 total).
  5. Negative weights are refused.
  6. compute_score uses the custom weights: with weights 0/40/60
     and scores 100/50/50, final = 100*0 + 50*0.40 + 50*0.60 = 50.
  7. compute_score falls back to 60/25/15 defaults when no override
     is set (same fixture, unset weights, should get 100*0.60 +
     50*0.25 + 50*0.15 = 80).
  8. Different employees on the same cycle can carry different
     weight sets independently.
  9. Editing weights on a non-OPEN cycle raises.
 10. Weights survive round-trip through the /weights POST route.
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

    existing = Company.query.filter_by(name="__WEIGHTS_AUDIT__").first()
    if existing:
        _teardown(existing.id)
    c = Company(name="__WEIGHTS_AUDIT__", base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    def _mk_user(email):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        return u

    owner = _mk_user("weights-owner@x.test")
    e_a = Employee(company_id=c.id, name="محمد",
                    status=EmployeeStatus.ACTIVE)
    e_b = Employee(company_id=c.id, name="خديجة",
                    status=EmployeeStatus.ACTIVE)
    db.session.add_all([e_a, e_b])
    db.session.commit()
    _STATE.update(company_id=c.id, owner_id=owner.id,
                    a_id=e_a.id, b_id=e_b.id)


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cycles_sql = "SELECT id FROM evaluation_cycles WHERE company_id = :c"
        for tbl in ("employee_category_weights",
                     "employee_evaluations",
                     "employee_metric_actuals",
                     "employee_targets",
                     "metric_log_entries"):
            try:
                conn.execute(text(
                    f"DELETE FROM {tbl} WHERE cycle_id IN ({cycles_sql})"),
                    {"c": company_id})
            except Exception:
                pass
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
            "DELETE FROM users WHERE email LIKE 'weights-%@x.test'"))


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _fresh_cycle(name="w"):
    from app.services.evaluation import create_cycle
    return create_cycle(
        company_id=_STATE["company_id"], name=name,
        period_type="MONTHLY",
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        created_by_id=_STATE["owner_id"],
    )


def _seed_score_fixture(cycle, emp_id, target_100, exec_50, growth_50):
    """Set up targets + actuals so that:
       - target_score = target_100 (single 100-weight metric)
       - execution_score = exec_50
       - growth_score = growth_50
    """
    from app.services.evaluation import upsert_target, upsert_actual
    upsert_target(cycle=cycle, employee_id=emp_id,
                    metric_key="t", target_value=100,
                    weight_pct=100, category="TARGET_ACHIEVEMENT")
    upsert_actual(cycle=cycle, employee_id=emp_id,
                    metric_key="t", actual_value=target_100)
    upsert_target(cycle=cycle, employee_id=emp_id,
                    metric_key="e", target_value=100,
                    weight_pct=100, category="EXECUTION_QUALITY")
    upsert_actual(cycle=cycle, employee_id=emp_id,
                    metric_key="e", actual_value=exec_50)
    upsert_target(cycle=cycle, employee_id=emp_id,
                    metric_key="g", target_value=100,
                    weight_pct=100, category="GROWTH")
    upsert_actual(cycle=cycle, employee_id=emp_id,
                    metric_key="g", actual_value=growth_50)


# ─── Checks ────────────────────────────────────────────────────────────
@check("1. get_category_weights returns 60/25/15 defaults on empty override")
def _():
    from app.services.evaluation import get_category_weights
    c = _fresh_cycle("defaults")
    w = get_category_weights(c.id, _STATE["a_id"])
    assert w == {"TARGET_ACHIEVEMENT": 60, "EXECUTION_QUALITY": 25,
                    "GROWTH": 15}, f"got {w}"
    return "defaults resolved"


@check("2. set_category_weights persists the three rows")
def _():
    from app.services.evaluation import set_category_weights
    from app.models import EmployeeCategoryWeight
    c = _fresh_cycle("persist")
    set_category_weights(c, _STATE["a_id"], {
        "TARGET_ACHIEVEMENT": 0, "EXECUTION_QUALITY": 40,
        "GROWTH": 60,
    })
    rows = EmployeeCategoryWeight.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["a_id"]).all()
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    return "3 rows persisted"


@check("3. get_category_weights returns saved values after set")
def _():
    from app.services.evaluation import (
        set_category_weights, get_category_weights,
    )
    c = _fresh_cycle("readback")
    set_category_weights(c, _STATE["a_id"], {
        "TARGET_ACHIEVEMENT": 40, "EXECUTION_QUALITY": 40,
        "GROWTH": 20,
    })
    w = get_category_weights(c.id, _STATE["a_id"])
    assert w["TARGET_ACHIEVEMENT"] == 40.0
    assert w["EXECUTION_QUALITY"] == 40.0
    assert w["GROWTH"] == 20.0
    return "round-trip 40/40/20"


@check("4. sum-to-100 invariant is enforced")
def _():
    from app.services.evaluation import (
        set_category_weights, EvaluationError,
    )
    c = _fresh_cycle("sum")
    raised = False
    try:
        set_category_weights(c, _STATE["a_id"], {
            "TARGET_ACHIEVEMENT": 30, "EXECUTION_QUALITY": 30,
            "GROWTH": 30,   # sums to 90
        })
    except EvaluationError as e:
        raised = True
        msg = str(e)
    assert raised
    assert "100" in msg
    return "sum=90 refused"


@check("5. negative weights refused")
def _():
    from app.services.evaluation import (
        set_category_weights, EvaluationError,
    )
    c = _fresh_cycle("neg")
    raised = False
    try:
        set_category_weights(c, _STATE["a_id"], {
            "TARGET_ACHIEVEMENT": -10, "EXECUTION_QUALITY": 50,
            "GROWTH": 60,
        })
    except EvaluationError:
        raised = True
    assert raised
    return "negative refused"


@check("6. compute_score uses custom weights (0/40/60)")
def _():
    """Fixture: target=100, exec=50, growth=50, weights=0/40/60.
    final = 100*0 + 50*0.40 + 50*0.60 = 0 + 20 + 30 = 50.0"""
    from app.services.evaluation import (
        set_category_weights, compute_score,
    )
    c = _fresh_cycle("custom-blend")
    _seed_score_fixture(c, _STATE["a_id"], 100, 50, 50)
    set_category_weights(c, _STATE["a_id"], {
        "TARGET_ACHIEVEMENT": 0, "EXECUTION_QUALITY": 40, "GROWTH": 60,
    })
    ev = compute_score(c, _STATE["a_id"])
    assert abs(float(ev.final_score) - 50.0) < 0.01, \
        f"expected 50.0, got {ev.final_score}"
    return f"final={float(ev.final_score)}"


@check("7. compute_score falls back to defaults when no override")
def _():
    """Fixture: target=100, exec=50, growth=50, NO override.
    final = 100*0.60 + 50*0.25 + 50*0.15 = 60 + 12.5 + 7.5 = 80.0"""
    from app.services.evaluation import compute_score
    c = _fresh_cycle("default-blend")
    _seed_score_fixture(c, _STATE["b_id"], 100, 50, 50)
    ev = compute_score(c, _STATE["b_id"])
    assert abs(float(ev.final_score) - 80.0) < 0.01, \
        f"expected 80.0, got {ev.final_score}"
    return f"final={float(ev.final_score)}"


@check("8. two employees on same cycle carry independent weight sets")
def _():
    from app.services.evaluation import (
        set_category_weights, get_category_weights,
    )
    c = _fresh_cycle("multi-emp")
    set_category_weights(c, _STATE["a_id"], {
        "TARGET_ACHIEVEMENT": 0, "EXECUTION_QUALITY": 40, "GROWTH": 60,
    })
    set_category_weights(c, _STATE["b_id"], {
        "TARGET_ACHIEVEMENT": 40, "EXECUTION_QUALITY": 40, "GROWTH": 20,
    })
    w_a = get_category_weights(c.id, _STATE["a_id"])
    w_b = get_category_weights(c.id, _STATE["b_id"])
    assert w_a["TARGET_ACHIEVEMENT"] == 0
    assert w_a["GROWTH"] == 60
    assert w_b["TARGET_ACHIEVEMENT"] == 40
    assert w_b["GROWTH"] == 20
    return "a=(0/40/60), b=(40/40/20) — independent"


@check("9. editing weights on non-OPEN cycle raises")
def _():
    from app.services.evaluation import (
        set_category_weights, transition_status, EvaluationError,
    )
    c = _fresh_cycle("frozen")
    transition_status(c, "SUBMITTED")
    raised = False
    try:
        set_category_weights(c, _STATE["a_id"], {
            "TARGET_ACHIEVEMENT": 33.34, "EXECUTION_QUALITY": 33.33,
            "GROWTH": 33.33,
        })
    except EvaluationError:
        raised = True
    assert raised
    return "SUBMITTED cycle refuses weight edits"


@check("10. weights survive round-trip through /weights POST route")
def _():
    from flask import current_app
    c = _fresh_cycle("http")
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    r = client.post(
        f"/evaluations/{c.id}/employees/{_STATE['a_id']}/weights",
        data={"w_target": "20", "w_execution": "30", "w_growth": "50"},
        follow_redirects=False,
    )
    assert r.status_code == 302, f"status={r.status_code}"
    from app.services.evaluation import get_category_weights
    w = get_category_weights(c.id, _STATE["a_id"])
    assert w["TARGET_ACHIEVEMENT"] == 20.0
    assert w["EXECUTION_QUALITY"] == 30.0
    assert w["GROWTH"] == 50.0
    return "POST → 302 + weights persisted"


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
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
