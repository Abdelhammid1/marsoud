#!/usr/bin/env python3
"""MARSOUD-TRIAL-DAYS-SETTING (Abdelhamid 2026-07-22).

`DEFAULT_SUBSCRIPTION_DAYS` used to be a hardcoded constant (30 days)
that Abdelhamid couldn't tune without a redeploy. Now stored under
`subscription_trial_days` in `platform_settings`, default 14, editable
from /admin/subscription-settings, and read at signup time.

Checks:
  1. Default value is 14 (the ticket's requested default).
  2. Setting a value through set_trial_days() round-trips through
     get_trial_days().
  3. Invalid values (0, 366, "abc") don't break get_trial_days() —
     it falls back to the default.
  4. activate_default_subscription() actually consults get_trial_days()
     (a fixture change to 7 → new company expires_at is +7 days).
  5. The admin form POST saves the value.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Skip boot-time orphan sweep — it's unrelated to this audit.
import os
os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _reset_setting():
    """Nuke the trial_days key so each test starts with the default."""
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM platform_settings WHERE key = "
            "'subscription_trial_days'"))


@check("1. Default trial length = 14 days (per ticket)")
def _():
    from app.services.subscription import (
        DEFAULT_SUBSCRIPTION_DAYS, get_trial_days,
    )
    _reset_setting()
    assert DEFAULT_SUBSCRIPTION_DAYS == 14, \
        f"DEFAULT_SUBSCRIPTION_DAYS = {DEFAULT_SUBSCRIPTION_DAYS}"
    assert get_trial_days() == 14, f"get_trial_days() = {get_trial_days()}"
    return "14 days"


@check("2. set_trial_days round-trips through get_trial_days")
def _():
    from app.services.subscription import set_trial_days, get_trial_days
    _reset_setting()
    set_trial_days(21)
    db.session.commit()
    assert get_trial_days() == 21
    set_trial_days(7)
    db.session.commit()
    assert get_trial_days() == 7
    return "21 → 7 round-trip OK"


@check("3. Invalid setting values fall back to the default")
def _():
    from app.services.subscription import get_trial_days
    from app.models import PlatformSetting
    _reset_setting()
    # Directly poke a bogus value into the setting.
    for bogus in ("abc", "0", "-5", "9999", ""):
        s = PlatformSetting(key="subscription_trial_days", value=bogus)
        db.session.add(s); db.session.commit()
        n = get_trial_days()
        assert n == 14, f"bogus={bogus!r} → get_trial_days()={n} (want 14)"
        _reset_setting()
    return "invalid values ignored"


@check("4. activate_default_subscription consults get_trial_days")
def _():
    from app.services.subscription import (
        activate_default_subscription, set_trial_days,
    )
    from app.models import Company
    _reset_setting()
    set_trial_days(3)
    db.session.commit()
    c = Company(name="__TRIAL_DAYS_A__", base_currency="EGP")
    before = datetime.utcnow()
    activate_default_subscription(c)
    delta = (c.subscription_expires_at - before).days
    assert 2 <= delta <= 3, (
        f"expected ~3d window, got {delta}d "
        f"(start={c.subscription_started_at} expires={c.subscription_expires_at})")
    db.session.rollback()  # don't persist the fixture Company
    return f"new company expires_at = now + {delta}d"


@check("5. Admin form POST saves the value")
def _():
    from flask import current_app
    from werkzeug.security import generate_password_hash
    from app.models import User
    from app.services.subscription import get_trial_days
    # Log in as a super-admin via the test client.
    admin_email = "trial-days-superadmin@x.test"
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = :e"), {"e": admin_email})
    u = User(
        email=admin_email,
        password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
        full_name="trial-days-super", is_superadmin=True,
    )
    db.session.add(u); db.session.commit()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    _reset_setting()
    r = client.post(
        "/admin/subscription-settings",
        data={
            "reminder_thresholds": "7,5,3,1,0",
            "grace_days": "7",
            "trial_days": "10",
            "readonly_enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, f"POST → {r.status_code}"
    assert get_trial_days() == 10, f"get_trial_days = {get_trial_days()}"
    _reset_setting()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :e"),
                      {"e": admin_email})
    return "POST → trial_days=10 persisted"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
