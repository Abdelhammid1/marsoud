#!/usr/bin/env python3
"""MARSOUD-METRIC-LOG-REPORT — real-browser verify.

Seeds a couple of MetricLogEntry rows against the demo company, then
opens /reports/metric-logs in Chromium and asserts the aggregated
numbers actually render.
"""
import os, sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "metric_log_report"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up():
    from app import create_app, db
    from app.models import (
        Company, User, Employee, EmployeeStatus, Department,
        EvaluationCycle, EvaluationCyclePeriod, EvaluationCycleStatus,
        MetricLogEntry,
    )
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        # Wipe prior fixtures.
        MetricLogEntry.query.filter(
            MetricLogEntry.metric_key.like('PW-MLR-%')).delete()
        Employee.query.filter(Employee.name.like('PW-MLR-%')).delete()
        EvaluationCycle.query.filter(
            EvaluationCycle.name.like('PW-MLR-%')).delete()
        db.session.commit()

        dept = Department.query.filter_by(
            company_id=company.id).first()
        if not dept:
            dept = Department(company_id=company.id, name="ops")
            db.session.add(dept); db.session.flush()

        emp = Employee(
            company_id=company.id, name="PW-MLR-Employee",
            department_id=dept.id, job_title="Analyst",
            status=EmployeeStatus.ACTIVE,
        )
        db.session.add(emp); db.session.flush()

        cycle = EvaluationCycle(
            company_id=company.id, name="PW-MLR-Cycle",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            period_type=EvaluationCyclePeriod.MONTHLY.value,
            status=EvaluationCycleStatus.OPEN.value,
        )
        db.session.add(cycle); db.session.flush()

        for day, val in [
            (date(2026, 7, 10), 80),
            (date(2026, 7, 11), 100),
            (date(2026, 7, 15), 120),
        ]:
            db.session.add(MetricLogEntry(
                company_id=company.id, cycle_id=cycle.id,
                employee_id=emp.id, metric_key="PW-MLR-calls",
                entry_date=day, value=val,
                entered_by_id=owner.id,
            ))
        db.session.commit()
        return {'emp_name': emp.name}


def _cleanup():
    from app import create_app, db
    from app.models import (
        Employee, EvaluationCycle, MetricLogEntry,
    )
    app = create_app()
    with app.app_context():
        MetricLogEntry.query.filter(
            MetricLogEntry.metric_key.like('PW-MLR-%')).delete()
        Employee.query.filter(Employee.name.like('PW-MLR-%')).delete()
        EvaluationCycle.query.filter(
            EvaluationCycle.name.like('PW-MLR-%')).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    seed = _spin_up()
    passed = failed = 0
    fails = []

    def _record(ok, label, details=""):
        nonlocal passed, failed
        if ok:
            print(f"PASS  {label}")
            passed += 1
        else:
            print(f"FAIL  {label}  ⇒ {details}")
            failed += 1
            fails.append(f"{label}: {details}")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(
                viewport={"width": 1600, "height": 1000}, locale="ar",
            )
            page = ctx.new_page()

            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            page.goto(f"{BASE}/reports/metric-logs",
                       wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_report.png"),
                            full_page=True)
            body = page.content()
            _record(
                "PW-MLR-Employee" in body,
                "1. report shows the fixture employee",
                "employee name missing",
            )
            _record(
                "PW-MLR-calls" in body,
                "2. report shows the metric_key",
                "metric name missing",
            )
            _record(
                "PW-MLR-Cycle" in body,
                "3. report shows the cycle name",
                "cycle name missing",
            )
            # sum = 300, avg = 100, latest = 120
            _record(
                "300" in body and "120" in body,
                "4. report shows aggregated numbers (sum + latest)",
                "aggregates missing",
            )
            b.close()
    finally:
        _cleanup()
        print()
        print(f"────  {passed} passed, {failed} failed  ────")
        if fails:
            for line in fails:
                print(f"  · {line}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
