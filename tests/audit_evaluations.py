#!/usr/bin/env python3
"""MARSOUD-EVALUATIONS — audit for the monthly-evaluation layer.

Coverage:
  1. create_cycle rejects bad inputs (empty name, start > end).
  2. transition_status enforces the OPEN → SUBMITTED → LOCKED
     state machine (illegal hops raise).
  3. Editing targets/actuals on a non-OPEN cycle raises.
  4. Score math: 60/25/15 blend with weighted category averages.
  5. Achievement cap: actual/target > 1.2 saturates at 120%.
  6. Bonus tier boundaries: 59.99→ZERO, 60→PARTIAL, 80→FULL, 100→EXCEEDED.
  7. Missing actual on a weighted target counts as 0 (no gaming).
  8. compute_score is idempotent — running it twice on the same data
     overwrites the same row.
  9. Cross-tenant: cycle in company A cannot be edited from company B's
     context (route _cycle_or_404 raises).
"""
import sys
from datetime import date, timedelta
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

    existing_a = Company.query.filter_by(name="__EVAL_AUDIT_A__").first()
    if existing_a:
        _teardown(existing_a.id)
    existing_b = Company.query.filter_by(name="__EVAL_AUDIT_B__").first()
    if existing_b:
        _teardown(existing_b.id)

    a = Company(name="__EVAL_AUDIT_A__", base_currency="SAR")
    b = Company(name="__EVAL_AUDIT_B__", base_currency="SAR")
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
            user_id=u.id, company_id=company_id, role="owner",
        ))
        return u

    owner_a = _mk_user("eval-a@x.test", a.id)
    owner_b = _mk_user("eval-b@x.test", b.id)

    emp_a = Employee(company_id=a.id, name="فؤاد",
                       status=EmployeeStatus.ACTIVE,
                       basic_salary=Decimal("5000"))
    emp_b = Employee(company_id=b.id, name="ماجد",
                       status=EmployeeStatus.ACTIVE,
                       basic_salary=Decimal("5000"))
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
        for tbl in ("employee_evaluations", "employee_metric_actuals",
                     "employee_targets", "evaluation_cycles"):
            try:
                conn.execute(
                    text(f"DELETE FROM {tbl} WHERE cycle_id IN "
                          f"(SELECT id FROM evaluation_cycles WHERE company_id = :c)"),
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
        conn.execute(text("DELETE FROM users WHERE email LIKE 'eval-%@x.test'"))


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                 "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _fresh_cycle(company_id, name="فحص"):
    from app.services.evaluation import create_cycle
    return create_cycle(
        company_id=company_id, name=name,
        period_type="MONTHLY",
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        created_by_id=_STATE["owner_a_id"],
    )


# ─── Service-layer checks ─────────────────────────────────────────────
@check("1. create_cycle rejects empty name and start > end")
def _():
    from app.services.evaluation import create_cycle, EvaluationError
    for kwargs in (
        {"name": "", "start_date": date(2026, 7, 1),
         "end_date": date(2026, 7, 31)},
        {"name": "ok", "start_date": date(2026, 8, 1),
         "end_date": date(2026, 7, 1)},
    ):
        raised = False
        try:
            create_cycle(
                company_id=_STATE["company_a_id"],
                period_type="MONTHLY",
                created_by_id=_STATE["owner_a_id"],
                **kwargs,
            )
        except EvaluationError:
            raised = True
        assert raised, f"expected EvaluationError for {kwargs}"
    return "both invalid inputs refused"


@check("2. transition_status enforces the OPEN→SUBMITTED→LOCKED SM")
def _():
    from app.services.evaluation import transition_status, EvaluationError
    c = _fresh_cycle(_STATE["company_a_id"], "trans")
    # OPEN can go to SUBMITTED
    transition_status(c, "SUBMITTED")
    assert c.status == "SUBMITTED"
    # SUBMITTED cannot skip straight to a bogus state
    raised = False
    try:
        transition_status(c, "BOGUS")
    except EvaluationError:
        raised = True
    assert raised
    # SUBMITTED → LOCKED is legal
    transition_status(c, "LOCKED")
    assert c.status == "LOCKED"
    # LOCKED → OPEN is legal (admin reopen)
    transition_status(c, "OPEN")
    assert c.status == "OPEN"
    # OPEN → LOCKED (skipping SUBMITTED) is NOT legal
    raised = False
    try:
        transition_status(c, "LOCKED")
    except EvaluationError:
        raised = True
    assert raised
    return "SM enforced"


@check("3. editing targets/actuals on non-OPEN cycle raises")
def _():
    from app.services.evaluation import (
        create_cycle, transition_status,
        upsert_target, upsert_actual, EvaluationError,
    )
    c = _fresh_cycle(_STATE["company_a_id"], "frozen")
    transition_status(c, "SUBMITTED")
    for fn, kwargs in (
        (upsert_target, dict(cycle=c, employee_id=_STATE["emp_a_id"],
                              metric_key="x", target_value=10,
                              weight_pct=50, category="TARGET_ACHIEVEMENT")),
        (upsert_actual, dict(cycle=c, employee_id=_STATE["emp_a_id"],
                              metric_key="x", actual_value=5)),
    ):
        raised = False
        try:
            fn(**kwargs)
        except EvaluationError:
            raised = True
        assert raised, f"expected EvaluationError for {fn.__name__}"
    return "SUBMITTED cycle blocks target + actual edits"


@check("4. score math: 60/25/15 blend + weighted averages")
def _():
    """One target per category, all achievement=100%, weight=100.
    final = 100*0.60 + 100*0.25 + 100*0.15 = 100."""
    from app.services.evaluation import (
        upsert_target, upsert_actual, compute_score,
    )
    c = _fresh_cycle(_STATE["company_a_id"], "math")
    for key, cat in [
        ("leads_won", "TARGET_ACHIEVEMENT"),
        ("code_quality", "EXECUTION_QUALITY"),
        ("skill_growth", "GROWTH"),
    ]:
        upsert_target(cycle=c, employee_id=_STATE["emp_a_id"],
                       metric_key=key, target_value=10,
                       weight_pct=100, category=cat)
        upsert_actual(cycle=c, employee_id=_STATE["emp_a_id"],
                       metric_key=key, actual_value=10,
                       entered_by_id=_STATE["owner_a_id"])
    ev = compute_score(c, _STATE["emp_a_id"])
    assert float(ev.target_score) == 100.0, f"target={ev.target_score}"
    assert float(ev.execution_score) == 100.0
    assert float(ev.growth_score) == 100.0
    assert float(ev.final_score) == 100.0
    # 100 → EXCEEDED
    assert ev.bonus_tier == "EXCEEDED"
    return f"all 100 → final=100 → EXCEEDED"


@check("5. achievement cap saturates at 120% (actual/target > 1.2)")
def _():
    from app.services.evaluation import (
        upsert_target, upsert_actual, compute_score,
    )
    c = _fresh_cycle(_STATE["company_a_id"], "cap")
    # Target=10, actual=100 → raw ratio=1000%, capped to 120%.
    upsert_target(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="x", target_value=10,
                   weight_pct=100, category="TARGET_ACHIEVEMENT")
    upsert_actual(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="x", actual_value=100)
    ev = compute_score(c, _STATE["emp_a_id"])
    assert float(ev.target_score) == 120.0, \
        f"expected 120 (capped), got {ev.target_score}"
    # final = 120 * 0.60 + 0 + 0 = 72
    assert abs(float(ev.final_score) - 72.0) < 0.01, \
        f"final={ev.final_score}"
    return f"raw 1000% saturated to 120%; final=72"


@check("6. bonus-tier boundaries: 59.99=ZERO, 60=PARTIAL, 80=FULL, 100=EXCEEDED")
def _():
    """Direct-test the internal tier resolver at every boundary."""
    from app.services.evaluation import _bonus_tier_for
    from app.models import BonusTier
    cases = [
        (Decimal("0"), BonusTier.ZERO),
        (Decimal("59.99"), BonusTier.ZERO),
        (Decimal("60"), BonusTier.PARTIAL),
        (Decimal("79.99"), BonusTier.PARTIAL),
        (Decimal("80"), BonusTier.FULL),
        (Decimal("99.99"), BonusTier.FULL),
        (Decimal("100"), BonusTier.EXCEEDED),
        (Decimal("120"), BonusTier.EXCEEDED),
    ]
    for score_val, expected in cases:
        got = _bonus_tier_for(score_val)
        assert got == expected, \
            f"{score_val} → {got}, expected {expected}"
    return f"{len(cases)} boundaries all correct"


@check("7. missing actual on weighted target counts as 0")
def _():
    """Set two targets, only enter one actual — the missing one
    contributes 0 to the weighted average, dragging the score down."""
    from app.services.evaluation import (
        upsert_target, upsert_actual, compute_score,
    )
    c = _fresh_cycle(_STATE["company_a_id"], "missing")
    # Two targets of equal weight (50 each) in TARGET_ACHIEVEMENT.
    # One actual = target (100%), the other missing (counts as 0).
    # → target_score = (100*50 + 0*50) / 100 = 50
    upsert_target(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="a", target_value=10, weight_pct=50,
                   category="TARGET_ACHIEVEMENT")
    upsert_target(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="b", target_value=10, weight_pct=50,
                   category="TARGET_ACHIEVEMENT")
    upsert_actual(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="a", actual_value=10)
    # NOTE: no actual for "b"
    ev = compute_score(c, _STATE["emp_a_id"])
    assert abs(float(ev.target_score) - 50.0) < 0.01, \
        f"expected 50, got {ev.target_score}"
    return f"missing actual counted as 0 (target_score=50 not 100)"


@check("8. compute_score is idempotent — re-runs overwrite same row")
def _():
    from app.services.evaluation import (
        upsert_target, upsert_actual, compute_score,
    )
    from app.models import EmployeeEvaluation
    c = _fresh_cycle(_STATE["company_a_id"], "idem")
    upsert_target(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="x", target_value=10, weight_pct=100,
                   category="TARGET_ACHIEVEMENT")
    upsert_actual(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="x", actual_value=5)
    ev1 = compute_score(c, _STATE["emp_a_id"])
    row1_id = ev1.id
    assert float(ev1.target_score) == 50.0

    # Change the actual, re-compute — SAME row, updated numbers.
    upsert_actual(cycle=c, employee_id=_STATE["emp_a_id"],
                   metric_key="x", actual_value=10)
    ev2 = compute_score(c, _STATE["emp_a_id"])
    assert ev2.id == row1_id, \
        f"expected same row id {row1_id}, got {ev2.id}"
    assert float(ev2.target_score) == 100.0
    count = EmployeeEvaluation.query.filter_by(
        cycle_id=c.id, employee_id=_STATE["emp_a_id"]).count()
    assert count == 1, f"expected 1 row, got {count}"
    return "same row updated; count=1"


@check("9. cross-tenant: route _cycle_or_404 blocks company B from A's cycle")
def _():
    from flask import current_app
    c = _fresh_cycle(_STATE["company_a_id"], "cross")
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_b_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_b_id"]
    r = client.get(f"/evaluations/{c.id}", follow_redirects=False)
    assert r.status_code == 404, \
        f"expected 404 (cross-tenant), got {r.status_code}"
    return "cross-tenant read blocked with 404"


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
