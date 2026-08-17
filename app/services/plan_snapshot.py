"""MARSOUD-PLAN-SSOT (2026-08-17) — Single source of truth for
"what plan is this company on right now?".

Every reader — super-admin table row, super-admin company detail card,
tenant settings/usage page, /api/v1/me API response, dashboard banner,
mobile app — MUST call `plan_snapshot(company)` and read from the
returned dict. No template or route may render the legacy
`Company.plan` String column (default `"FREE"`), which is deprecated
as of this ticket. The word "Free" never appears in the UI because
there is no Free plan in the system.

Trial is a subscription STATUS, not a plan. A company can be on
Growth+Trial or Growth+Active; the plan is the same. If neither
`subscription_plan` nor `intended_plan` is set, the company has NO
plan (yet) and the status is `no_plan` — never "Free".

This wraps two lower-level services:
  · plan_gating.plan_allows()      — enforcement (unchanged)
  · subscription.subscription_state() — lifecycle oracle

and adds one thing on top: the plan/status/access_mode triple every
UI reader needs, in a single dict.
"""
from datetime import date, datetime


# Public labels (Arabic). Kept in this file so callers don't need to
# import from company_360.py which drags in a lot of unrelated code.
STATUS_LABELS_AR = {
    "trial":     "تجريبي",
    "active":    "نشط",
    "warning":   "تحذير — انتهى الاشتراك",
    "read_only": "قراءة فقط",
    "no_plan":   "بلا باقة",
}

# Bootstrap-style pill classes used in the super-admin templates.
STATUS_BADGE_CLASSES = {
    "trial":     "badge-sent",
    "active":    "badge-paid",
    "warning":   "badge-partial",
    "read_only": "badge-overdue",
    "no_plan":   "badge-draft",
}


def _resolve_plan(company):
    """Effective plan = subscription_plan → intended_plan → None.

    Matches the fallback in plan_gating.plan_allows so display + gating
    never disagree.
    """
    if company is None:
        return None
    return (getattr(company, "subscription_plan", None)
            or getattr(company, "intended_plan", None))


def plan_snapshot(company) -> dict:
    """The canonical answer to 'what plan is this company on?'

    Keys returned (all always present, some nullable):
      plan_code                  e.g. "growth" | None
      plan_name_ar               e.g. "Growth" | None
      status                     "trial" | "active" | "warning" | "read_only" | "no_plan"
      status_label_ar            Arabic label for the badge
      status_badge_class         CSS class to attach to the badge span
      access_mode                "full" | "warning" | "read_only" | "no_plan"
      trial_ends_at              datetime | None (for trial only)
      subscription_started_at    datetime | None
      subscription_expires_at    datetime | None
      warning_days_left          int | None (during grace only)
      is_trial                   bool convenience
      is_read_only               bool convenience
    """
    if company is None:
        return _empty_snapshot()

    plan = _resolve_plan(company)
    if plan is None:
        return _empty_snapshot()

    from app.services.subscription import subscription_state
    st = subscription_state(company)

    started = getattr(company, "subscription_started_at", None)
    expires = getattr(company, "subscription_expires_at", None)
    next_billing = getattr(company, "next_billing_date", None)

    # ── Derive status from the (state, has_ever_paid) pair. ──────
    # subscription_state returns 'active' both for pre-expiry trials
    # AND for post-payment paid subs. We distinguish by "has next_billing_date
    # ever been set" — the saas_billing cron writes it after the first
    # paid invoice. Before that, `active` inside the trial window is a Trial.
    lifecycle = st["state"]
    if lifecycle == "active":
        # Pre-expiry: either trial (never paid) or paid + renewed.
        if next_billing is None:
            status = "trial"
        else:
            status = "active"
    elif lifecycle == "grace":
        status = "warning"
    elif lifecycle == "read_only":
        status = "read_only"
    else:
        status = "active"  # future-proof fallback

    access_mode = {
        "trial":     "full",
        "active":    "full",
        "warning":   "warning",
        "read_only": "read_only",
    }[status]

    # Grace-period countdown for the "N days left before read-only" chip.
    warning_days_left = None
    if status == "warning" and st.get("grace_end"):
        try:
            warning_days_left = max(
                0, (st["grace_end"] - date.today()).days)
        except Exception:
            warning_days_left = None

    return {
        "plan_code":     plan.code,
        "plan_name_ar":  plan.name_ar or plan.name or plan.code,
        "status":        status,
        "status_label_ar":    STATUS_LABELS_AR[status],
        "status_badge_class": STATUS_BADGE_CLASSES[status],
        "access_mode":   access_mode,
        "trial_ends_at": expires if status == "trial" else None,
        "subscription_started_at": started,
        "subscription_expires_at": expires,
        "warning_days_left":      warning_days_left,
        "is_trial":     status == "trial",
        "is_read_only": status == "read_only",
    }


def _empty_snapshot() -> dict:
    """The "no plan yet" shape — used when the company has neither a
    promoted `plan_id` nor an `intended_plan_id`. Rendered as بلا باقة
    in every UI. NEVER rendered as "Free" — that plan does not exist in
    Marsoud."""
    return {
        "plan_code":     None,
        "plan_name_ar":  None,
        "status":        "no_plan",
        "status_label_ar":    STATUS_LABELS_AR["no_plan"],
        "status_badge_class": STATUS_BADGE_CLASSES["no_plan"],
        "access_mode":   "no_plan",
        "trial_ends_at": None,
        "subscription_started_at": None,
        "subscription_expires_at": None,
        "warning_days_left":      None,
        "is_trial":     False,
        "is_read_only": False,
    }
