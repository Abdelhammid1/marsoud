#!/usr/bin/env python3
"""MARSOUD-FIX-CHOOSE-PLAN-SUBMIT (Abdelhamid 2026-07-25).

Bug: after verifying the email and picking a plan on /choose-plan,
the user was NOT redirected to the dashboard — the submit button
lived OUTSIDE the plan form and its JS fallback
(document.querySelector('form').submit()) picked the header
LOGOUT form we added when hide_sidebar was introduced. Result:
"pick plan" silently logged the user out.

Checks:
  1. Rendered /choose-plan template has the confirm button INSIDE
     the choose-plan form (structural regression guard).
  2. POST /choose-plan with a valid plan_id sets
     company.intended_plan_id and redirects to dashboard.index.
  3. Middleware require_plan_selection stops redirecting once
     intended_plan_id is set.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

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


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CP_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'cp-%@x.test'"))


def _bootstrap():
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    _teardown()
    c = Company(name="__CP_CO__", base_currency="EGP",
                 subdomain="cp-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="cp-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="cp-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u


@check("1. Confirm button lives INSIDE the choose-plan form")
def _():
    """Regression guard: read the template file directly and prove
    the submit button is after id='choose-plan-form' and before
    the closing </form>. The old layout put the button after the
    </form> and used a JS querySelector trick — that broke when
    another form (logout) was added to the page."""
    from pathlib import Path
    tpl = Path("app/templates/auth/choose_plan.html").read_text(
        encoding="utf-8")
    form_start = tpl.find('id="choose-plan-form"')
    form_end = tpl.find('</form>', form_start)
    submit = tpl.find('type="submit"', form_start)
    assert form_start > 0, "form id not found"
    assert form_end > form_start, "form never closed"
    assert form_start < submit < form_end, \
        "submit button is NOT between form open and close"
    # And no JS querySelector fallback survives.
    assert "querySelector('form').submit()" not in tpl, \
        "JS submit trick still present — the bug will come back"
    return "structural OK"


@check("2. POST /choose-plan → sets intended_plan_id + redirects to dashboard")
def _():
    from flask import current_app
    from app.models import Company, Plan
    c, u = _bootstrap()
    plan = Plan.query.filter_by(is_active=True).first()
    if plan is None:
        # Seed a stub so the test can run on a bare DB.
        plan = Plan(code="cp-stub", name="Stub", name_ar="نجربة",
                     is_active=True, price_monthly=100)
        db.session.add(plan); db.session.commit()
    _STATE["c"] = c; _STATE["u"] = u; _STATE["plan"] = plan

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.post("/choose-plan", data={"plan_id": plan.id},
                     follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"expected redirect, got {r.status_code}"
    # dashboard.index maps to /home in the current URL layout. What
    # matters is: not /choose-plan, not /login, and not another dead
    # end.
    loc = r.headers.get("Location") or ""
    assert loc, "empty Location"
    assert "/choose-plan" not in loc, \
        f"loop back to /choose-plan: {loc}"
    assert "/login" not in loc, f"kicked to /login: {loc}"
    # Reload from DB — intended_plan_id must be set.
    fresh = db.session.get(Company, c.id)
    db.session.refresh(fresh)
    assert fresh.intended_plan_id == plan.id, \
        f"intended_plan_id not saved (got {fresh.intended_plan_id})"
    return f"redirect → {loc}"


@check("3. require_plan_selection middleware no longer traps after choice")
def _():
    from flask import current_app
    from app.models import User
    u = db.session.get(User, _STATE["u"].id)
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["c"].id
    # Any dashboard page — must NOT redirect back to /choose-plan.
    r = client.get("/", follow_redirects=False)
    loc = r.headers.get("Location") or ""
    assert "/choose-plan" not in loc, \
        f"middleware STILL sends to /choose-plan: {loc}"
    return "middleware clears user through"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
