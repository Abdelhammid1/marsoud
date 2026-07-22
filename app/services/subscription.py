"""MARSOUD subscription lifecycle service.

Centralizes:
  - default subscription duration for newly created companies
  - reading platform-level settings (reminder thresholds, grace days,
    read-only toggle)
  - computing the current state of a company's subscription
    (active / grace / read_only)

The settings live in `platform_settings` (key/value) — abdelhamid can
change them from /admin/subscription-settings without redeploy. Defaults
in this module are the safety net for first-boot before the admin opens
that page.
"""
from datetime import datetime, timedelta, date
from app import db


# MARSOUD-TRIAL-DAYS-SETTING (Abdelhamid 2026-07-22) — the trial length
# used to be hardcoded at 30 days. It's now stored under the
# `subscription_trial_days` key in `platform_settings` and editable from
# /admin/subscription-settings. This constant is the fallback used only
# when the key is missing (first boot, before the admin visits the page).
DEFAULT_SUBSCRIPTION_DAYS = 14

# Defaults for the platform-level settings. Used when the key isn't set
# in the platform_settings table.
DEFAULT_REMINDER_THRESHOLDS = [7, 5, 3, 1, 0]
DEFAULT_GRACE_DAYS = 7
DEFAULT_READONLY_ENABLED = True


def get_trial_days():
    """MARSOUD-TRIAL-DAYS-SETTING — trial window for new companies.
    Reads `subscription_trial_days` from platform_settings, falling
    back to DEFAULT_SUBSCRIPTION_DAYS when unset or invalid."""
    raw = _get_setting_raw("subscription_trial_days")
    if raw and raw.strip().isdigit():
        n = int(raw)
        if 1 <= n <= 365:
            return n
    return DEFAULT_SUBSCRIPTION_DAYS


def set_trial_days(n):
    """Persist the trial window. Callers should already have validated
    the int + range; we clamp defensively."""
    n = max(1, min(365, int(n)))
    _set_setting_raw("subscription_trial_days", str(n))


def activate_default_subscription(company, *, plan_code="enterprise"):
    """Stamp a newly-created company with default subscription dates and
    plan. Safe to call before or after the row is flushed. No commit —
    the caller controls the transaction."""
    from app.models import Plan
    now = datetime.utcnow()
    if not company.subscription_started_at:
        company.subscription_started_at = now
    if not company.subscription_expires_at:
        # MARSOUD-TRIAL-DAYS-SETTING — pull the trial length from
        # platform_settings so admins can tune it without a redeploy.
        company.subscription_expires_at = now + timedelta(days=get_trial_days())
    if not company.plan_id and plan_code:
        plan = Plan.query.filter_by(code=plan_code).first()
        if plan:
            company.plan_id = plan.id


# ─── Platform-level settings (key/value) ────────────────────────────
def _get_setting_raw(key):
    """Return the raw String value of a platform_setting, or None."""
    try:
        from app.models import PlatformSetting
        row = PlatformSetting.query.filter_by(key=key).first()
        return row.value if row else None
    except Exception:
        # Table might not exist yet during initial migration runs.
        return None


def _set_setting_raw(key, value):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.session.add(PlatformSetting(
            key=key, value=value, updated_at=datetime.utcnow(),
        ))


def get_reminder_thresholds():
    """Sorted-desc list of N values: days before expiry to send a reminder.
    0 means "on the day of expiry"."""
    raw = _get_setting_raw("subscription_reminder_thresholds")
    if not raw:
        return list(DEFAULT_REMINDER_THRESHOLDS)
    out = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.lstrip("-").isdigit():
            n = int(piece)
            if 0 <= n <= 365:
                out.append(n)
    return sorted(set(out), reverse=True) if out else list(DEFAULT_REMINDER_THRESHOLDS)


def set_reminder_thresholds(values):
    """`values` is an iterable of ints. Stored as comma-separated."""
    cleaned = sorted({int(v) for v in values if 0 <= int(v) <= 365},
                     reverse=True)
    _set_setting_raw("subscription_reminder_thresholds",
                     ",".join(str(v) for v in cleaned))


def get_grace_days():
    raw = _get_setting_raw("subscription_grace_days")
    if raw and raw.lstrip("-").isdigit():
        n = int(raw)
        if 0 <= n <= 365:
            return n
    return DEFAULT_GRACE_DAYS


def set_grace_days(n):
    _set_setting_raw("subscription_grace_days", str(int(n)))


def get_readonly_enabled():
    raw = _get_setting_raw("subscription_readonly_enabled")
    if raw is None:
        return DEFAULT_READONLY_ENABLED
    return raw.lower() in ("1", "true", "yes", "on")


def set_readonly_enabled(enabled):
    _set_setting_raw("subscription_readonly_enabled",
                     "true" if enabled else "false")


# ─── Subscription state computation ────────────────────────────────
def subscription_state(company, *, today=None):
    """Return a dict describing the company's current subscription state.

    Result keys:
      state            'active' | 'grace' | 'read_only'
      days_remaining   int — days until expiry (positive) or into grace
                       (negative). None if no expiry is set.
      banner           True when the UI should show a warning banner.
      grace_end        the date the read-only kicks in (or None).
      read_only_enabled  whether the global toggle is on.
    """
    today = today or date.today()
    expires_at = getattr(company, "subscription_expires_at", None)
    readonly = get_readonly_enabled()

    if not expires_at:
        return {"state": "active", "days_remaining": None,
                "banner": False, "grace_end": None,
                "read_only_enabled": readonly}

    expires_date = expires_at.date() if hasattr(expires_at, "date") else expires_at
    grace_days = get_grace_days()
    grace_end = expires_date + timedelta(days=grace_days)

    if today < expires_date:
        days_remaining = (expires_date - today).days
        return {"state": "active", "days_remaining": days_remaining,
                "banner": False, "grace_end": grace_end,
                "read_only_enabled": readonly}

    if today < grace_end:
        days_into_grace = (today - expires_date).days
        return {"state": "grace", "days_remaining": -days_into_grace,
                "banner": True, "grace_end": grace_end,
                "read_only_enabled": readonly}

    # Past grace. If the toggle is off, we stay in "grace" UX (warning
    # banner only). If on, read-only takes over.
    if readonly:
        return {"state": "read_only", "days_remaining": (today - expires_date).days * -1,
                "banner": True, "grace_end": grace_end,
                "read_only_enabled": True}
    return {"state": "grace", "days_remaining": (today - expires_date).days * -1,
            "banner": True, "grace_end": grace_end,
            "read_only_enabled": False}


# ─── Read-only enforcement ────────────────────────────────────────
# Endpoint names that bypass the read-only block. Add to this list when
# a new feature needs to keep working post-expiry (e.g. payment widget).
READONLY_EXEMPT_ENDPOINTS = {
    # Auth flow must always work
    "auth.login", "auth.logout", "auth.register", "auth.forgot_password",
    "auth.reset_password",
    # The company's own renewal page (so they can self-serve) + super-admin
    "portal_emp.change_password",
    # All superadmin.* endpoints are exempt by a prefix check, see is_endpoint_exempt
}


def is_endpoint_exempt(endpoint):
    """Whether the given endpoint should bypass the read-only block."""
    if not endpoint:
        return True
    if endpoint in READONLY_EXEMPT_ENDPOINTS:
        return True
    # Whole blueprints exempt: super-admin + cron tick.
    for prefix in ("superadmin.", "cron."):
        if endpoint.startswith(prefix):
            return True
    return False
