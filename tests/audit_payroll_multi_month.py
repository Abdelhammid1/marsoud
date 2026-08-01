#!/usr/bin/env python3
"""MARSOUD-PAYROLL-MONTH-PREVIEW (Abdelhamid 2026-08-01).

Batch 9 Ticket 4. `/payroll/run` opened on the current month
only. Year/month dropdowns lived inside the POST form so
changing them did nothing until submit — which posted the run.
Split into a mini GET form (preview) + POST form (commit). The
backend already reads year/month from request.args on GET; the
fix is template-only.

Checks:
  1. Template has 2 <form> tags with distinct methods (GET + POST).
  2. The GET form's action points to payroll.run.
  3. The POST form has hidden year/month inputs carrying the
     currently-previewed month.
  4. GET /payroll/run?year=2026&month=5 returns 200 + preview
     for May 2026 (year/month propagate correctly into the
     rendered form).
  5. Submit button copy includes the currently-previewed month
     so users see WHAT they're about to commit.
"""
import os
import sys
from datetime import date, datetime
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
            "SELECT id FROM companies WHERE name LIKE '__PMP_%__'"))]
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
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'pmp-%@x.test'"))


def _seed_owner():
    from app.models import (
        Company, User, UserStatus, Plan,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    # Pick a plan that includes the `hr` module (payroll is
    # gated on it). Pro is a safe default across seed variants.
    plan = None
    for candidate in Plan.query.filter_by(is_active=True).all():
        if "hr" in (candidate.modules or []):
            plan = candidate
            break
    if plan is None:
        plan = Plan.query.filter_by(is_active=True).first()
    c = Company(name="__PMP_A__", base_currency="EGP",
                 subdomain="pmp-a",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 intended_plan_id=plan.id if plan else None,
                 plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="pmp-owner@x.test",
             password_hash=generate_password_hash(
                 "TestPass123!", method="pbkdf2:sha256"),
             full_name="pmp-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u


@check("1. Template has 2 <form> tags (GET + POST)")
def _():
    src = (ROOT / "app" / "templates" / "payroll"
            / "run_form.html").read_text()
    assert 'method="GET"' in src, \
        "GET form missing — preview flow not wired"
    assert 'method="POST"' in src, \
        "POST form missing (regression)"
    return "both forms present"


@check("2. GET form's action points to payroll.run")
def _():
    src = (ROOT / "app" / "templates" / "payroll"
            / "run_form.html").read_text()
    assert "url_for('payroll.run')" in src, \
        "GET form action missing url_for('payroll.run')"
    return "GET form correctly targeted"


@check("3. POST form has hidden year/month inputs")
def _():
    src = (ROOT / "app" / "templates" / "payroll"
            / "run_form.html").read_text()
    # After splitting, the year/month need to travel with POST
    # via hidden inputs so the commit posts against the
    # currently-previewed month.
    assert 'type="hidden" name="year"' in src, \
        "hidden year input missing on POST form"
    assert 'type="hidden" name="month"' in src, \
        "hidden month input missing on POST form"
    return "hidden year/month carry state to POST"


@check("4. GET /payroll/run?year=2026&month=5 renders preview for May")
def _():
    from flask import current_app
    _teardown()
    c, u = _seed_owner()
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
            sess["active_company_id"] = c.id
        r = client.get("/payroll/run?year=2026&month=5")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    # Hidden year/month must contain 2026 + 5, and the submit
    # copy must mention "5/2026".
    assert 'name="year" value="2026"' in body, \
        "year didn't propagate into hidden input"
    assert 'name="month" value="5"' in body, \
        "month didn't propagate into hidden input"
    return "May 2026 preview renders"


@check("5. Submit button copy names the previewed month")
def _():
    src = (ROOT / "app" / "templates" / "payroll"
            / "run_form.html").read_text()
    # Copy should interpolate month/year (not just say "شهر")
    assert "{{ month }}/{{ year }}" in src, \
        "submit button doesn't name the previewed month"
    return "button copy names the month"


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
