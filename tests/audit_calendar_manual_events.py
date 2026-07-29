#!/usr/bin/env python3
"""MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29).

Batch 5 Ticket 5 audit. Users can add / edit / delete their own
events on /calendar/, and the manual events show up alongside
the derived (meeting / task / project) events.

Checks:
  1. create_event() route persists a CalendarEvent with the right
     fields.
  2. edit_event() updates fields on the same row.
  3. delete_event() soft-deletes (is_deleted=True), doesn't purge.
  4. Cross-tenant: company A's event is NOT visible in the
     calendar aggregation for company B.
  5. Window filter: an event outside the default 30-day window is
     hidden until ?days=90.
  6. Invalid input (missing title / bad datetime / ends_at <
     starts_at) is rejected without a row insert.
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
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CAL_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'cal-%@x.test'"))


def _mk_owner(suffix):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan.query.first()
    c = Company(name=f"__CAL_{suffix}__", base_currency="EGP",
                 subdomain=f"cal-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"cal-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"cal-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return u, c


def _post(url, user, company, form=None):
    """Direct handler invocation via test_request_context — same
    pattern that stabilized the coupon audit in Ticket 4."""
    from flask import current_app, g as _g
    from flask_login import login_user
    fresh_c = db.session.get(type(company), company.id)
    fresh_u = db.session.get(type(user), user.id)
    with current_app.test_request_context(url, method="POST",
                                             data=form or {}):
        login_user(fresh_u)
        _g.active_company = fresh_c
        _g.user_companies = [fresh_c]
        # Find the view function by URL (avoids importing every
        # blueprint handler by name).
        endpoint, values = current_app.url_map.bind("localhost").match(
            url.split("?")[0], method="POST")
        view = current_app.view_functions[endpoint]
        return view(**values)


@check("1. create_event → CalendarEvent row persisted with all fields")
def _():
    from app.models import CalendarEvent
    _teardown()
    u, c = _mk_owner("A")
    _STATE["u"] = u; _STATE["c"] = c
    starts = datetime.utcnow() + timedelta(days=3, hours=10)
    _post("/calendar/events", u, c, {
        "title": "اجتماع تجريبي",
        "starts_at": starts.strftime("%Y-%m-%dT%H:%M"),
        "location": "غرفة الاجتماعات",
        "description": "مناقشة الميزانية",
        "reminder_minutes_before": "15",
    })
    ev = CalendarEvent.query.filter_by(company_id=c.id).first()
    assert ev is not None, "no CalendarEvent row created"
    assert ev.title == "اجتماع تجريبي"
    assert ev.location == "غرفة الاجتماعات"
    assert ev.description == "مناقشة الميزانية"
    assert ev.reminder_minutes_before == 15
    assert ev.created_by_id == u.id
    assert ev.is_deleted is False
    _STATE["event_id"] = ev.id
    return f"event #{ev.id} '{ev.title}' saved"


@check("2. edit_event → fields updated in place")
def _():
    from app.models import CalendarEvent
    u, c = _STATE["u"], _STATE["c"]
    eid = _STATE["event_id"]
    new_start = datetime.utcnow() + timedelta(days=5, hours=14)
    _post(f"/calendar/events/{eid}/edit", u, c, {
        "title": "اجتماع محدّث",
        "starts_at": new_start.strftime("%Y-%m-%dT%H:%M"),
        "location": "زووم",
    })
    db.session.expire_all()
    ev = db.session.get(CalendarEvent, eid)
    assert ev.title == "اجتماع محدّث", f"title={ev.title}"
    assert ev.location == "زووم"
    # Description was omitted from the edit form, should be cleared.
    assert ev.description is None
    return "title + location + description all updated"


@check("3. delete_event → soft-deleted (is_deleted=True, row stays)")
def _():
    from app.models import CalendarEvent
    u, c = _STATE["u"], _STATE["c"]
    eid = _STATE["event_id"]
    _post(f"/calendar/events/{eid}/delete", u, c)
    db.session.expire_all()
    ev = db.session.get(CalendarEvent, eid)
    assert ev is not None, "row was hard-deleted"
    assert ev.is_deleted is True, "is_deleted stayed False"
    return "soft-delete flag set, row preserved for audit"


@check("4. Cross-tenant: company A's event NOT visible to company B's calendar")
def _():
    from flask import current_app, g as _g
    from flask_login import login_user
    from app.models import CalendarEvent
    _teardown()
    u_a, c_a = _mk_owner("A")
    u_b, c_b = _mk_owner("B")
    # A creates an event.
    starts = datetime.utcnow() + timedelta(days=2, hours=9)
    db.session.add(CalendarEvent(
        company_id=c_a.id, created_by_id=u_a.id,
        title="event-in-A",
        starts_at=starts))
    db.session.commit()
    # B loads /calendar/ — should see zero events.
    with current_app.test_request_context("/calendar/", method="GET"):
        login_user(db.session.get(type(u_b), u_b.id))
        _g.active_company = db.session.get(type(c_b), c_b.id)
        _g.user_companies = [_g.active_company]
        from app.routes.calendar import index as _idx
        resp = _idx()
    body = resp if isinstance(resp, str) else resp
    assert "event-in-A" not in body, \
        "cross-tenant leak: A's event visible to B"
    return "no cross-tenant leak"


@check("5. Window filter: event 60 days out is hidden at ?days=30 but shown at ?days=90")
def _():
    from flask import current_app, g as _g
    from flask_login import login_user
    from app.models import CalendarEvent
    _teardown()
    u, c = _mk_owner("W")
    far_starts = datetime.utcnow() + timedelta(days=60, hours=10)
    db.session.add(CalendarEvent(
        company_id=c.id, created_by_id=u.id,
        title="far-future-event",
        starts_at=far_starts))
    db.session.commit()
    from app.routes.calendar import index as _idx
    with current_app.test_request_context("/calendar/?days=30",
                                            method="GET"):
        login_user(db.session.get(type(u), u.id))
        _g.active_company = db.session.get(type(c), c.id)
        _g.user_companies = [_g.active_company]
        body30 = _idx()
    with current_app.test_request_context("/calendar/?days=90",
                                            method="GET"):
        login_user(db.session.get(type(u), u.id))
        _g.active_company = db.session.get(type(c), c.id)
        _g.user_companies = [_g.active_company]
        body90 = _idx()
    assert "far-future-event" not in body30, \
        "event beyond 30d window incorrectly shown at ?days=30"
    assert "far-future-event" in body90, \
        f"event within 90d window missing at ?days=90"
    return "window filter honored"


@check("6. Invalid input → rejected without row insert")
def _():
    from app.models import CalendarEvent
    _teardown()
    u, c = _mk_owner("X")
    before = CalendarEvent.query.filter_by(company_id=c.id).count()
    # a) missing title
    _post("/calendar/events", u, c, {
        "starts_at": (datetime.utcnow() + timedelta(days=1)
                       ).strftime("%Y-%m-%dT%H:%M"),
    })
    # b) missing starts_at
    _post("/calendar/events", u, c, {"title": "no-date"})
    # c) unparseable date
    _post("/calendar/events", u, c, {
        "title": "bad-date",
        "starts_at": "not-a-date",
    })
    # d) ends_at before starts_at
    start = datetime.utcnow() + timedelta(days=2)
    end = start - timedelta(hours=1)
    _post("/calendar/events", u, c, {
        "title": "reversed-times",
        "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
        "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
    })
    after = CalendarEvent.query.filter_by(company_id=c.id).count()
    assert after == before, \
        f"invalid inputs leaked into DB: before={before} after={after}"
    return f"all 4 invalid inputs rejected ({before} rows unchanged)"


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
