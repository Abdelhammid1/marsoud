#!/usr/bin/env python3
"""MARSOUD-INACTIVE-COMPANIES-MONITORING (Abdelhamid 2026-07-22).

Companies.last_activity_at + /admin/companies/inactive filter view.

Checks:
  1. Fresh Company row has last_activity_at = NULL.
  2. start_session stamps last_activity_at on the company_id passed in.
  3. /admin/companies/inactive?since=3d filters correctly.
  4. ?since=never returns companies with NULL last_activity_at.
  5. Soft-deleted companies are excluded from the list.
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
            "SELECT id FROM companies WHERE name LIKE '__INAC_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'inac-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_company(suffix, activity_delta_days=None, deleted=False):
    """Fresh company with optional last_activity_at seeded to
    (now - N days). None → NULL last_activity_at."""
    from app.models import Company
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__INAC_{suffix}__", base_currency="EGP",
                subdomain=f"inac-{suffix.lower()}")
    activate_default_subscription(c, plan_code=None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    if activity_delta_days is not None:
        c.last_activity_at = datetime.utcnow() - timedelta(days=activity_delta_days)
    if deleted:
        c.deleted_at = datetime.utcnow()
    db.session.commit()
    return c


@check("1. Fresh Company row has last_activity_at = NULL")
def _():
    _teardown()
    c = _mk_company("FRESH")
    from app.models import Company
    row = Company.query.filter_by(name="__INAC_FRESH__").one()
    assert row.last_activity_at is None
    return "NULL by default"


@check("2. start_session stamps company.last_activity_at")
def _():
    from flask import current_app, g
    from app.models import Company, User
    from werkzeug.security import generate_password_hash
    from app.services.activity import start_session
    _teardown()
    c = _mk_company("STAMP")
    u = User(email="inac-stamp@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="s", is_active=True)
    db.session.add(u); db.session.commit()

    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    with current_app.test_request_context(
            headers={"User-Agent": "TestUA"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.1"}):
        # request context is required for _client_ip + parse_user_agent
        # inside start_session.
        start_session(u, company_id=c.id)

    db.session.expire_all()
    row = Company.query.get(c.id)
    assert row.last_activity_at is not None
    # Should be within the last minute.
    delta = (datetime.utcnow() - row.last_activity_at).total_seconds()
    assert delta < 60, f"stamp delta = {delta}s"
    return f"stamped ({int(delta)}s ago)"


@check("3. /admin/companies/inactive?since=3d filters companies "
       "with last_activity_at < now-3d")
def _():
    from flask import current_app, g
    from app.models import User
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'inac-super@x.test'"))
    _mk_company("D2", activity_delta_days=2)    # NOT inactive @ 3d
    _mk_company("D5", activity_delta_days=5)    # IS inactive @ 3d
    _mk_company("D10", activity_delta_days=10)  # IS inactive @ 3d
    admin = User(email="inac-super@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="inac-super", is_superadmin=True,
                 is_active=True)
    db.session.add(admin); db.session.commit()

    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    r = client.get("/admin/companies/inactive?since=3d")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "__INAC_D2__" not in body, "recent company must NOT be listed"
    assert "__INAC_D5__" in body
    assert "__INAC_D10__" in body
    _STATE["admin_id"] = admin.id
    return "3d filter selects only >3d-idle companies"


@check("4. ?since=never returns companies with NULL last_activity_at")
def _():
    from flask import current_app, g
    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["admin_id"])
        sess["_fresh"] = True
    r = client.get("/admin/companies/inactive?since=never")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # From check 1 we still have __INAC_FRESH__ with NULL activity —
    # wait, actually teardown ran in check 3. So there's no INAC_FRESH.
    # But new fresh company should appear.
    _mk_company("NEVER")
    r = client.get("/admin/companies/inactive?since=never")
    body = r.get_data(as_text=True)
    assert "__INAC_NEVER__" in body
    return "never-visited companies surfaced"


@check("5. Soft-deleted companies are excluded")
def _():
    from flask import current_app, g
    _mk_company("SOFTDEL", activity_delta_days=30, deleted=True)
    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["admin_id"])
        sess["_fresh"] = True
    r = client.get("/admin/companies/inactive?since=7d")
    body = r.get_data(as_text=True)
    assert "__INAC_SOFTDEL__" not in body, \
        "soft-deleted company should be excluded"
    return "soft-deleted excluded"


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
