"""MARSOUD-PLATFORM-REVENUE-DASHBOARD (Abdelhamid 2026-07-22).

Cross-tenant revenue + subscription metrics for /admin.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from app import db
from app.models import Company, Plan


def _now():
    return datetime.utcnow()


def mrr():
    """Monthly Recurring Revenue in EGP.

    Sum of plan.price_for("EGP", "monthly") for every company
    whose subscription hasn't expired yet. This intentionally
    counts trial companies too since their intended_plan is what
    the sales pipeline is chasing.
    """
    now = _now()
    total = Decimal("0")
    companies = Company.query.filter(
        Company.deleted_at.is_(None),
        Company.subscription_expires_at > now,
    ).all()
    for c in companies:
        plan = None
        if c.plan_id:
            plan = db.session.get(Plan, c.plan_id)
        if not plan and c.intended_plan_id:
            plan = db.session.get(Plan, c.intended_plan_id)
        if not plan:
            continue
        price = plan.price_for("EGP", "monthly")
        if price:
            total += Decimal(price)
    return total


def arr():
    return mrr() * Decimal(12)


def plan_distribution():
    """{plan.id → count} for every non-deleted company. Includes
    NULL plan as "unassigned"."""
    counts = {}
    companies = Company.query.filter(Company.deleted_at.is_(None)).all()
    for c in companies:
        pid = c.plan_id or c.intended_plan_id or 0
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def subscription_states():
    """Bucket companies into TRIAL / ACTIVE / GRACE / EXPIRED /
    NEVER_STARTED. Definitions:
      · TRIAL: subscription window active AND intended_plan_id is
        NULL (they haven't picked a paid plan yet).
      · ACTIVE: subscription_expires_at in the future AND a paid
        plan_id/intended_plan_id is set.
      · GRACE: expired within the last N days (uses
        get_grace_days() from subscription.py).
      · EXPIRED: past the grace window.
      · NEVER_STARTED: subscription_started_at IS NULL.
    """
    from app.services.subscription import get_grace_days
    now = _now()
    grace_days = get_grace_days()
    grace_cutoff = now - timedelta(days=grace_days)
    result = {"TRIAL": 0, "ACTIVE": 0, "GRACE": 0,
              "EXPIRED": 0, "NEVER_STARTED": 0}
    for c in Company.query.filter(Company.deleted_at.is_(None)).all():
        if not c.subscription_started_at:
            result["NEVER_STARTED"] += 1
            continue
        exp = c.subscription_expires_at
        if exp and exp > now:
            if not (c.plan_id or c.intended_plan_id):
                result["TRIAL"] += 1
            else:
                result["ACTIVE"] += 1
        elif exp and exp > grace_cutoff:
            result["GRACE"] += 1
        else:
            result["EXPIRED"] += 1
    return result


def renewals_due(days=7):
    """Count of companies whose subscription_expires_at falls in
    the next N days. Used to warn Ibrahim about upcoming renewals."""
    now = _now()
    cutoff = now + timedelta(days=days)
    return Company.query.filter(
        Company.deleted_at.is_(None),
        Company.subscription_expires_at > now,
        Company.subscription_expires_at <= cutoff,
    ).count()


def monthly_revenue_series(months=12):
    """Approximate 12-month revenue history: sum of plan monthly
    prices for companies whose subscription was active during the
    month. NOT invoice-accurate (we have no platform-invoice model
    for plan sales yet) but good enough for a "growth trend" chart.
    """
    now = _now()
    # Anchor at the 1st of the current month.
    first_of_month = now.replace(day=1, hour=0, minute=0,
                                  second=0, microsecond=0)
    series = []
    for i in range(months, 0, -1):
        # First of the month N months ago.
        month_start = _shift_months(first_of_month, -(i - 1))
        month_end = _shift_months(month_start, 1)
        egp = _revenue_for_window(month_start, month_end)
        series.append({
            "month": month_start.strftime("%Y-%m"),
            "egp": float(egp),
        })
    return series


def _revenue_for_window(start, end):
    total = Decimal("0")
    # A company contributes to a month if its subscription window
    # overlaps that month.
    companies = Company.query.filter(
        Company.deleted_at.is_(None),
        Company.subscription_started_at != None,
        Company.subscription_started_at < end,
    ).all()
    for c in companies:
        if c.subscription_expires_at and \
                c.subscription_expires_at < start:
            continue
        plan_id = c.plan_id or c.intended_plan_id
        if not plan_id:
            continue
        plan = db.session.get(Plan, plan_id)
        if not plan:
            continue
        price = plan.price_for("EGP", "monthly")
        if price:
            total += Decimal(price)
    return total


def _shift_months(dt, months):
    """dateutil.relativedelta is already installed (used by
    recurring_tasks). Wraps it so callers don't import it."""
    from dateutil.relativedelta import relativedelta
    return dt + relativedelta(months=months)
