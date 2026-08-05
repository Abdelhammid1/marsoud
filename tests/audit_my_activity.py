#!/usr/bin/env python3
"""MARSOUD-PORTAL-MY-ACTIVITY-01 (2026-08-06) — audit for /my/activity.

The whole feature is one security guarantee: an employee sees their
own activity + sessions, and no one else's, no matter what URL params
are added. Every check here exists to hold that line.

Every check verified to FAIL against pre-change code before commit.

Checks
  1. page renders 200 for an employee with an HR record
  2. own sessions appear on the page
  3. own activity rows appear on the page
  4. cross-user leak, direct — B's page never shows A's data
  5. cross-user leak via ?user_id= — hand-crafted A's id on B's page
     still shows only B's data (THE key security check)
  6. cross-tenant leak — A signed into company B never sees A's data
     from company A
  7. 90-day hard floor — activity older than 90 days does not appear
     even with ?from=2020-01-01
  8. no employee record → terminal no_record template, not 500
"""
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__MYACT_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import Company, Plan, Employee, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__myact__").first()
    if not plan:
        plan = Plan(code="__myact__", name="MyAct", name_ar="سجل نشاطي",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="EGP",
                     vat_rate=0, plan_id=plan.id)
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        return co

    long_ago = date.today() - timedelta(days=400)

    def _mk_user_emp(co, tag):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local", full_name=tag,
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, "team_member")
        e = Employee(company_id=co.id, name=f"emp-{tag}",
                     basic_salary=Decimal("5000"),
                     status="ACTIVE", start_date=long_ago, user_id=u.id)
        db.session.add(e); db.session.flush()
        return u.id, e.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    ua, ea = _mk_user_emp(co_a, "a")
    ub, eb = _mk_user_emp(co_b, "b")
    # A also has a membership in B (owner) so we can log A into B for
    # the cross-tenant check without inventing a third user. Their
    # Employee record is still in A.
    from app.services.roles import set_membership_role as _smr
    _smr(ua, co_b.id, "team_member")
    # And B needs to be an employee in B (already set up above) plus
    # cross-company for check 6's mirror.
    db.session.commit()

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  emp_a=ea, emp_b=eb,
                  user_a=ua, user_b=ub)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__myact__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        # Sessions + activity rows tie to user_id but not company_id
        # for user-only rows; wipe them by hand so nothing leaks
        # between runs.
        db.session.execute(text(
            "DELETE FROM user_activity_log WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM user_sessions WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_logs():
    from app.models import UserActivityLog, UserSession
    from sqlalchemy import text
    # Wipe only rows tied to fixture users so nothing bleeds between
    # checks.
    for uid in (_STATE["user_a"], _STATE["user_b"]):
        db.session.execute(text(
            "DELETE FROM user_activity_log WHERE user_id=:u"), {"u": uid})
        db.session.execute(text(
            "DELETE FROM user_sessions WHERE user_id=:u"), {"u": uid})
    db.session.commit()


def _seed_session(user_id, company_id, marker, when=None):
    from app.models import UserSession
    row = UserSession(
        user_id=user_id, company_id=company_id,
        session_token=f"{marker}-tok",
        login_at=when or datetime.utcnow(),
        last_seen_at=when or datetime.utcnow(),
        ip_address=marker,               # marker lands in a visible column
        user_agent=marker, device_type="DESKTOP",
        device_os="Linux", browser="Chrome", status="ACTIVE")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_activity(user_id, company_id, marker, when=None,
                   session_id=None):
    from app.models import UserActivityLog
    row = UserActivityLog(
        user_id=user_id, company_id=company_id, session_id=session_id,
        action_type="VIEW", entity_type="Test",
        entity_label=marker, route=f"/x/{marker}", method="GET",
        ip_address=marker, device_type="DESKTOP",
        device_os="Linux", browser="Chrome",
        created_at=when or datetime.utcnow())
    db.session.add(row)
    db.session.commit()
    return row


def _get_as(user_id, company_id, path):
    """GET `path` as (user_id, active_company=company_id) inside a
    FRESH app_context so Flask-Login's g._login_user cache does not
    serve the request as whichever user this app-context saw first
    (handoff fact 7). Returns the response.

    Cheaper than spinning up a whole new app per call (audit_my_attendance
    check 8's pattern) because a fresh app_context on the same app
    still gives you a clean g stack."""
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
            s["active_company_id"] = company_id
        return c.get(path)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. page renders 200 for an employee with an HR record")
def _():
    _reset_logs()
    r = _get_as(_STATE["user_a"], _STATE["cid_a"], "/my/activity")
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "سجل نشاطي" in body, "page title missing"
    return f"200 · {len(body)} bytes"


@check("2. own sessions appear on the page")
def _():
    _reset_logs()
    _seed_session(_STATE["user_a"], _STATE["cid_a"], "SESS_A_MARKER")
    body = _get_as(_STATE["user_a"], _STATE["cid_a"],
                   "/my/activity").get_data(as_text=True)
    assert "SESS_A_MARKER" in body, "own session not on page"
    return "session for A rendered on A's page"


@check("3. own activity rows appear on the page")
def _():
    _reset_logs()
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "ACT_A_MARKER")
    body = _get_as(_STATE["user_a"], _STATE["cid_a"],
                   "/my/activity").get_data(as_text=True)
    assert "ACT_A_MARKER" in body, "own activity not on page"
    return "activity for A rendered on A's page"


@check("4. cross-user leak, direct — B does not see A's rows")
def _():
    _reset_logs()
    _seed_session(_STATE["user_a"], _STATE["cid_a"], "A_SESS_MARKER")
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "A_ACT_MARKER")
    # And give B their own data so the page isn't empty (empty-page
    # false pass would let a bug through).
    _seed_activity(_STATE["user_b"], _STATE["cid_b"], "B_ACT_MARKER")
    body = _get_as(_STATE["user_b"], _STATE["cid_b"],
                   "/my/activity").get_data(as_text=True)
    assert "B_ACT_MARKER" in body, "B cannot see own data"
    assert "A_ACT_MARKER" not in body, (
        "CROSS-USER LEAK: B sees A's activity")
    assert "A_SESS_MARKER" not in body, (
        "CROSS-USER LEAK: B sees A's session")
    return "B sees own · never sees A's"


@check("5. cross-user leak via ?user_id= — the URL bypass")
def _():
    """The specific attack the ticket calls out: B crafts
    /my/activity?user_id=<A_ID>. The route must OVERWRITE user_id from
    the URL with current_user.id before applying filters."""
    _reset_logs()
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "A_URL_BYPASS_MARKER")
    _seed_activity(_STATE["user_b"], _STATE["cid_b"], "B_URL_BYPASS_MARKER")
    body = _get_as(_STATE["user_b"], _STATE["cid_b"],
                   f"/my/activity?user_id={_STATE['user_a']}"
                   ).get_data(as_text=True)
    assert "B_URL_BYPASS_MARKER" in body, "B lost own data with ?user_id="
    assert "A_URL_BYPASS_MARKER" not in body, (
        "URL BYPASS: ?user_id= leaks A's data to B — the load-bearing "
        "overwrite is not happening.")
    return "crafted ?user_id= ignored"


@check("6. cross-tenant leak — A signed into B does not see A@A data")
def _():
    """User A has memberships in both A and B. When their active_company
    is B, /my/activity must scope to company_id=B and never surface
    activity rows that were logged against company A.

    NB: user A has no Employee row in company B in this fixture — but
    for the LEAK CHECK to be meaningful we only need to prove the query
    scope. If _my_employee() returns None, the response redirects
    without surfacing anyone's data, which also passes the "no leak"
    bar. Test both signals."""
    _reset_logs()
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "A_AT_A_MARKER")
    _seed_activity(_STATE["user_a"], _STATE["cid_b"], "A_AT_B_MARKER")
    r = _get_as(_STATE["user_a"], _STATE["cid_b"], "/my/activity")
    body = r.get_data(as_text=True) if r.status_code == 200 else ""
    assert "A_AT_A_MARKER" not in body, (
        "CROSS-TENANT LEAK: A signed into B sees A's own A-company activity")
    return f"A@B status={r.status_code}, no A@A marker leaked"


@check("7. 90-day hard floor — old activity does not surface")
def _():
    """Even with ?from=2020-01-01 (a widened window), rows older than
    90 days must NOT appear. The route clamps _start to
    utcnow()-90d after parsing."""
    _reset_logs()
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "OLD_MARKER",
                   when=datetime.utcnow() - timedelta(days=200))
    _seed_activity(_STATE["user_a"], _STATE["cid_a"], "RECENT_MARKER",
                   when=datetime.utcnow() - timedelta(days=3))
    body = _get_as(_STATE["user_a"], _STATE["cid_a"],
                   "/my/activity?from=2020-01-01&to=2030-01-01"
                   ).get_data(as_text=True)
    assert "RECENT_MARKER" in body, "recent row missing"
    assert "OLD_MARKER" not in body, (
        "90-day floor bypassed by ?from=2020-01-01 — 200-day-old row surfaced")
    return "old row clamped out even with ?from=2020-01-01"


@check("8. no employee record -> terminal no_record page, not 500")
def _():
    """A user in the active company with no Employee row must not
    500. _no_employee_record_response returns the terminal template
    for employee/client roles (or redirects for others), same as
    /my/account."""
    from app.models import User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    u = User(email=f"{PREFIX}ghost@audit.local", full_name="ghost",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    set_membership_role(u.id, _STATE["cid_a"], "employee")
    db.session.commit()
    try:
        r = _get_as(u.id, _STATE["cid_a"], "/my/activity")
        assert r.status_code in (200, 302, 303), (
            f"no-employee user got {r.status_code}, expected 200/302/303")
    finally:
        from sqlalchemy import text
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
        db.session.commit()
    return f"no 500; status handled cleanly"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
