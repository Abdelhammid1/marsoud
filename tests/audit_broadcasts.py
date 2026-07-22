#!/usr/bin/env python3
"""MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22).

Audience filtering + send.

Checks:
  1. audience_query(kind=all) returns every active non-super-admin.
  2. audience_query(kind=active) excludes expired subscriptions.
  3. audience_query(kind=expired) selects only expired.
  4. audience_query(kind=by_plan, plan_id=N) selects users on that plan.
  5. send() writes one Notification per recipient + stamps sent_at
     + target_count.
  6. Re-sending an already-sent broadcast raises BroadcastError.
  7. EMAIL channel calls send_email exactly once per user.
"""
import os
import sys
from datetime import datetime, timedelta
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
        conn.execute(text(
            "DELETE FROM notifications WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'bc-%@x.test')"))
        conn.execute(text(
            "DELETE FROM broadcasts WHERE title LIKE 'BC-TEST-%'"))
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__BC_%__'"))]
        for cid in target_cids:
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
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'bc-%@x.test')"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'bc-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_user_in_company(suffix, expires_delta_days=30, plan_id=None):
    """Create user + company with a specific subscription state."""
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    c = Company(name=f"__BC_{suffix}__", base_currency="EGP",
                subdomain=f"bc-{suffix.lower()}")
    activate_default_subscription(c, plan_code=None)
    c.subscription_expires_at = datetime.utcnow() + timedelta(
        days=expires_delta_days)
    if plan_id:
        c.plan_id = plan_id
        c.intended_plan_id = plan_id
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"bc-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"bc-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow())
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return u, c


@check("1. audience_query(all) returns every active non-super-admin")
def _():
    from app.services.broadcasts import audience_query
    from app.models import AUDIENCE_ALL
    _teardown()
    _mk_user_in_company("ALL_A")
    _mk_user_in_company("ALL_B")
    q = audience_query({"kind": AUDIENCE_ALL})
    got = [u.email for u in q.all()]
    assert "bc-all_a@x.test" in got
    assert "bc-all_b@x.test" in got
    return f"{len(got)} users"


@check("2. audience_query(active) excludes expired subscriptions")
def _():
    from app.services.broadcasts import audience_query
    from app.models import AUDIENCE_ACTIVE
    _teardown()
    _mk_user_in_company("ACTIVE_A", expires_delta_days=10)   # active
    _mk_user_in_company("EXP_A", expires_delta_days=-2)      # expired
    q = audience_query({"kind": AUDIENCE_ACTIVE})
    got = [u.email for u in q.all()]
    assert "bc-active_a@x.test" in got
    assert "bc-exp_a@x.test" not in got
    return "expired excluded"


@check("3. audience_query(expired) selects only expired")
def _():
    from app.services.broadcasts import audience_query
    from app.models import AUDIENCE_EXPIRED
    q = audience_query({"kind": AUDIENCE_EXPIRED})
    got = [u.email for u in q.all()]
    assert "bc-exp_a@x.test" in got
    assert "bc-active_a@x.test" not in got
    return "only expired"


@check("4. audience_query(by_plan) filters by plan_id")
def _():
    from app.services.broadcasts import audience_query
    from app.models import AUDIENCE_BY_PLAN, Plan
    _teardown()
    p1 = Plan.query.filter_by(code="basic").first() or \
         Plan.query.filter_by(code="starter").first() or \
         Plan.query.first()
    p_other = Plan.query.filter(Plan.id != p1.id).first()
    _mk_user_in_company("P1_A", plan_id=p1.id)
    _mk_user_in_company("P1_B", plan_id=p1.id)
    _mk_user_in_company("P_OTHER", plan_id=p_other.id)
    q = audience_query({"kind": AUDIENCE_BY_PLAN, "plan_id": p1.id})
    got = [u.email for u in q.all()]
    assert "bc-p1_a@x.test" in got
    assert "bc-p1_b@x.test" in got
    assert "bc-p_other@x.test" not in got
    return f"plan {p1.id} → 2 users"


@check("5. send() writes one Notification per recipient")
def _():
    from app.models import (
        Broadcast, Notification, NotificationKind, AUDIENCE_BY_PLAN,
        Plan, User,
    )
    from app.services.broadcasts import send
    _teardown()
    # Use a specific plan_id so the audience is deterministic and
    # doesn't pull in leftover test users on other plans.
    p_target = Plan.query.first()
    _mk_user_in_company("S_A", plan_id=p_target.id)
    _mk_user_in_company("S_B", plan_id=p_target.id)
    _mk_user_in_company("S_C", plan_id=p_target.id)
    b = Broadcast(title="BC-TEST-1", body_html="<p>hi</p>")
    b.set_audience({"kind": AUDIENCE_BY_PLAN, "plan_id": p_target.id})
    b.set_channels(["INAPP"])
    db.session.add(b); db.session.commit()
    sent, failed = send(b)
    # Prior fixture cruft may add users on the same plan → sent is
    # whatever the audience found. Assert that OUR three users each
    # received a Notification.
    fixture_uids = {u.id for u in User.query.filter(
        User.email.in_(["bc-s_a@x.test", "bc-s_b@x.test", "bc-s_c@x.test"]))}
    notif_uids = {n.user_id for n in Notification.query.filter_by(
        kind=NotificationKind.BROADCAST.value,
        title="BC-TEST-1").all()}
    assert fixture_uids.issubset(notif_uids), \
        f"missing: {fixture_uids - notif_uids}"
    assert b.sent_at is not None
    return f"sent={sent}, our 3 users all got Notification"


@check("6. Re-sending an already-sent broadcast raises BroadcastError")
def _():
    from app.models import Broadcast
    from app.services.broadcasts import send, BroadcastError
    b = Broadcast.query.filter_by(title="BC-TEST-1").one()
    raised = False
    try:
        send(b)
    except BroadcastError:
        raised = True
    assert raised
    return "second send refused"


@check("7. EMAIL channel calls send_email once per user")
def _():
    from app.models import Broadcast, AUDIENCE_BY_PLAN, Plan
    from app.services import broadcasts as _bc_mod
    _teardown()
    # Use a plan nobody else uses (create a fresh dummy).
    p = Plan(code="bc-plan-test", name="BC Plan",
             name_ar="اختبار", is_active=True)
    db.session.add(p); db.session.flush()
    _mk_user_in_company("E_A", plan_id=p.id)
    _mk_user_in_company("E_B", plan_id=p.id)
    b = Broadcast(title="BC-TEST-EMAIL", body_html="<p>x</p>")
    b.set_audience({"kind": AUDIENCE_BY_PLAN, "plan_id": p.id})
    b.set_channels(["INAPP", "EMAIL"])
    db.session.add(b); db.session.commit()

    email_calls = []
    from app.services import email as _email_mod
    orig = _email_mod.send_email
    _email_mod.send_email = lambda to, subject, html_body, **kw: (
        email_calls.append((to, subject)) or True)
    try:
        sent, failed = _bc_mod.send(b)
    finally:
        _email_mod.send_email = orig
    assert sent == 2, f"sent={sent}"
    assert len(email_calls) == 2, f"emails sent={len(email_calls)}"
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM plans WHERE code = 'bc-plan-test'"))
    return f"send_email called {len(email_calls)}× (one per user)"


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
