#!/usr/bin/env python3
"""MARSOUD-QUOTAS (Abdelhamid 2026-07-22).

Generic Plan-Quotas: Users / AI Tokens (monthly) / Storage / Branches.
Each Quota row has included_amount + enforcement_mode
(BLOCK / ALLOW_NOTIFY / UNLIMITED) + optional price_per_extra_unit.

Owner-notification rail: at 80/90/100% under ALLOW_NOTIFY, fires an
in-app + email nudge; dedupe per (company, quota_type, threshold,
cycle_month) so we don't spam once per request.

Checks:
  1. is_unlimited() — no Quota row OR mode=UNLIMITED → True.
  2. check_quota with mode=UNLIMITED never raises.
  3. count_users returns active user_companies rows.
  4. count_branches returns child companies (parent_id=company.id).
  5. BLOCK mode: check_quota raises QuotaBlockedError when at the cap.
  6. ALLOW_NOTIFY mode: does NOT raise, but writes a
     QuotaNotificationSent row at the crossed threshold.
  7. log_ai_usage inserts an ai_token_usage row.
  8. count_ai_tokens_this_month sums only current-month rows.
  9. Per-employee AI cap: block a specific user even when the
     company still has budget.
 10. Notification dedupe: two calls in the same cycle don't fire
     two rows for the same threshold.
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
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__QUOTA_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'quota-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'quota-test-%'"))
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id NOT IN "
            "(SELECT id FROM plans)"))
        conn.execute(text(
            "DELETE FROM quota_notifications_sent WHERE company_id "
            "NOT IN (SELECT id FROM companies)"))


def _setup(quota_type=None, included=10, mode="BLOCK"):
    """Create a fresh company + plan + optional quota."""
    from app.models import Company, User, Plan, Quota
    from app.models.user import user_companies
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    import random

    slug = f"q{random.randint(1000,9999)}"
    plan = Plan(code=f"quota-test-{slug}", name="Quota-Test",
                name_ar="اختبار كوتا", is_active=True,
                price_monthly=0)
    db.session.add(plan); db.session.flush()

    c = Company(name=f"__QUOTA_{slug}__", base_currency="EGP",
                subdomain=f"quota-{slug}", plan_id=plan.id,
                intended_plan_id=plan.id)
    activate_default_subscription(c, plan_code=None)
    # Expire the trial so plan_gating doesn't override.
    c.subscription_expires_at = datetime.utcnow() - timedelta(days=1)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    owner = User(email=f"quota-owner-{slug}@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="q-owner", is_active=True)
    db.session.add(owner); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))

    if quota_type:
        q = Quota(plan_id=plan.id, quota_type=quota_type,
                  included_amount=included, enforcement_mode=mode)
        db.session.add(q)
    db.session.commit()
    return c, owner, plan


# ────────────────────────────────────────────────────────────────
@check("1. is_unlimited() — no Quota row → True, UNLIMITED → True")
def _():
    from app.services.quotas import is_unlimited, get_quota
    from app.models import QUOTA_USERS
    _teardown()
    c, _, _ = _setup()   # no quota row
    assert is_unlimited(get_quota(c, QUOTA_USERS))
    c2, _, _ = _setup(quota_type=QUOTA_USERS, mode="UNLIMITED")
    assert is_unlimited(get_quota(c2, QUOTA_USERS))
    return "no row + UNLIMITED both treated as unlimited"


@check("2. check_quota with UNLIMITED never raises")
def _():
    from app.services.quotas import check_quota
    from app.models import QUOTA_USERS
    c, _, _ = _setup()   # no quota
    check_quota(c, QUOTA_USERS, incoming=99999)   # doesn't raise
    return "unlimited quota does not raise"


@check("3. count_users returns active user_companies rows")
def _():
    from app.services.quotas import count_users
    c, _owner, _ = _setup()
    n = count_users(c)
    assert n == 1, f"expected 1 owner user, got {n}"
    return f"count_users = {n}"


@check("4. count_branches returns children")
def _():
    from app.services.quotas import count_branches
    from app.models import Company
    c, _, _ = _setup()
    child = Company(name=f"{c.name}-CHILD", base_currency="EGP",
                     subdomain=f"{c.subdomain}-child",
                     parent_id=c.id)
    db.session.add(child); db.session.commit()
    n = count_branches(c)
    assert n == 1, f"got {n}"
    return "1 child counted"


@check("5. BLOCK mode raises at cap")
def _():
    from app.services.quotas import check_quota, QuotaBlockedError
    from app.models import QUOTA_USERS
    c, _, _ = _setup(quota_type=QUOTA_USERS, included=1, mode="BLOCK")
    # 1 owner already → cap = 1 → incoming=1 pushes over.
    raised = False
    try:
        check_quota(c, QUOTA_USERS, incoming=1)
    except QuotaBlockedError:
        raised = True
    assert raised, "expected QuotaBlockedError"
    return "BLOCK mode refuses at cap"


@check("6. ALLOW_NOTIFY mode does NOT raise + records notification")
def _():
    from app.services.quotas import check_quota
    from app.models import QUOTA_USERS, QuotaNotificationSent
    c, _, _ = _setup(quota_type=QUOTA_USERS, included=1,
                      mode="ALLOW_NOTIFY")
    check_quota(c, QUOTA_USERS, incoming=1)
    # Should have fired 100% notification.
    n = QuotaNotificationSent.query.filter_by(
        company_id=c.id).count()
    assert n >= 1, f"expected >=1 notif, got {n}"
    return f"notification row saved (n={n})"


@check("7. log_ai_usage inserts a row")
def _():
    from app.services.quotas import log_ai_usage
    from app.models import AiTokenUsage
    c, owner, _ = _setup()
    log_ai_usage(c.id, owner.id, "anthropic",
                 "claude-sonnet-4-5", 100, 200)
    row = AiTokenUsage.query.filter_by(company_id=c.id).one()
    assert row.total_tokens == 300, f"got {row.total_tokens}"
    return "usage row inserted with total=300"


@check("8. count_ai_tokens_this_month sums current month only")
def _():
    from app.services.quotas import (
        log_ai_usage, count_ai_tokens_this_month,
    )
    c, owner, _ = _setup()
    log_ai_usage(c.id, owner.id, "anthropic", "claude", 50, 50)
    log_ai_usage(c.id, owner.id, "anthropic", "claude", 10, 20)
    n = count_ai_tokens_this_month(c)
    assert n == 130, f"got {n}"
    return f"sum = {n}"


@check("9. Per-employee AI cap blocks the user (even when co has "
       "budget)")
def _():
    from app.services.quotas import (
        check_quota, log_ai_usage, QuotaBlockedError,
    )
    from app.models import (
        QUOTA_AI_TOKENS_MONTH, EmployeeAiCap,
    )
    c, owner, plan = _setup(quota_type=QUOTA_AI_TOKENS_MONTH,
                              included=10_000, mode="BLOCK")
    # Employee already used 800.
    log_ai_usage(c.id, owner.id, "anthropic", "claude", 400, 400)
    # Cap the employee at 1000.
    db.session.add(EmployeeAiCap(
        company_id=c.id, user_id=owner.id, monthly_cap=1000))
    db.session.commit()
    # Employee tries to burn 300 more → 800+300=1100 > 1000. Block.
    raised = False
    try:
        check_quota(c, QUOTA_AI_TOKENS_MONTH,
                     incoming=300, user_id=owner.id)
    except QuotaBlockedError:
        raised = True
    assert raised, "employee cap should block"
    return "per-employee cap enforced"


@check("10. Notification dedupe — same threshold in same cycle only "
       "fires once")
def _():
    from app.services.quotas import check_quota
    from app.models import QUOTA_USERS, QuotaNotificationSent
    c, _, _ = _setup(quota_type=QUOTA_USERS, included=1,
                      mode="ALLOW_NOTIFY")
    check_quota(c, QUOTA_USERS, incoming=1)
    n1 = QuotaNotificationSent.query.filter_by(
        company_id=c.id).count()
    check_quota(c, QUOTA_USERS, incoming=1)
    n2 = QuotaNotificationSent.query.filter_by(
        company_id=c.id).count()
    assert n2 == n1, f"dedupe broken: {n1} → {n2}"
    return f"dedupe held ({n1} row(s))"


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
