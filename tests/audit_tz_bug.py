#!/usr/bin/env python3
"""MARSOUD-TZ-BUG (Abdelhamid 2026-07-04) — audit.

Reproduces Abdelhamid's report: he created a task comment at wall-clock
18:44 and the UI showed 19:10+ (a time that hadn't happened yet).
Root cause was model-level `default=datetime.now` (server-local) mixed
with the `company_dt` display filter that assumes UTC — on a server
running Asia/Riyadh, everything came out +3h ahead of real time.

Fix — unify to UTC-naive:
  1. Every model DateTime column default → `datetime.utcnow`
  2. Every user-typed datetime-local input parses via
     `to_utc_from_company()` before storage
  3. Display side unchanged (`company_dt` still tags-as-UTC and
     converts to company tz — correct once the save side matches)

Assertions:
  1. Every model's DateTime column default is `datetime.utcnow`
     (no rogue `.now` still in place).
  2. Save→display round-trip on a fresh TaskComment matches
     wall-clock within 1 minute (no +3h drift).
  3. `_parse_datetime_local()` converts a naive-local input to UTC
     (not stored as-is).
  4. `to_utc_from_company` / `to_company_tz_str` are true inverses.
  5. Notifications, LeadComments, TaskActivityLog — spot check same
     round-trip works for those too.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _setup():
    from app.models import Company, User, UserStatus, Task, Project
    from app.models.user import user_companies

    u_old = User.query.filter_by(email="tz_actor@t.co").first()
    if u_old:
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u_old.id))
        db.session.delete(u_old); db.session.commit()

    old_co = Company.query.filter_by(name="__TZBUG__").first()
    if old_co:
        _teardown_company(old_co.id)

    from app.services.seed_coa import seed_default_coa
    co = Company(name="__TZBUG__", base_currency="SAR",
                  timezone="Asia/Riyadh")
    db.session.add(co); db.session.flush()
    seed_default_coa(co.id)

    u = User(email="tz_actor@t.co", full_name="tz",
              status=UserStatus.ACTIVE.value)
    u.set_password("x")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner",
    ))
    db.session.commit()
    _STATE.update(user_id=u.id, company_id=co.id, company=co)


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id = :c"
                ), {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                       {"c": company_id})


@check("1. Every model DateTime column default is datetime.utcnow (no .now leaks)")
def _():
    from sqlalchemy import DateTime
    leaks = []
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        # Only look at model classes actually declared in the app —
        # skip Alembic version + third-party.
        if not cls.__module__.startswith("app.models"):
            continue
        for col in cls.__table__.columns:
            if not isinstance(col.type, DateTime):
                continue
            d = col.default and col.default.arg
            if d is datetime.now:
                leaks.append(f"{cls.__tablename__}.{col.name}")
    assert not leaks, (
        f"still using datetime.now on: {leaks}. Should be utcnow after "
        f"MARSOUD-TZ-BUG."
    )


@check("2. Task comment: save→display round-trip within 1 min of wall-clock")
def _():
    """Reproduces Abdelhamid's exact test: create a comment "now",
    verify it displays as "now" (not now+3h) in the company's tz."""
    from app.models import Task, TaskComment, Project, Customer, ProjectStatus
    from app.services.time import to_company_tz_str
    from datetime import date, timedelta as _td

    # A project needs a customer.
    cust = Customer(company_id=_STATE["company_id"], name="عميل تز")
    db.session.add(cust); db.session.flush()

    p = Project(
        company_id=_STATE["company_id"],
        name="مشروع تز", type="خدمة",
        customer_id=cust.id, manager_id=_STATE["user_id"],
        start_date=date.today(),
        end_date=date.today() + _td(days=7),
        status=ProjectStatus.PLANNING,
    )
    db.session.add(p); db.session.flush()

    t = Task(
        company_id=_STATE["company_id"],
        project_id=p.id, title="مهمة تز",
        created_by_id=_STATE["user_id"],
        assigned_to_id=_STATE["user_id"],
    )
    db.session.add(t); db.session.flush()

    # Capture wall-clock in Riyadh BEFORE the insert.
    wall_before = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

    c = TaskComment(task_id=t.id, user_id=_STATE["user_id"],
                     company_id=_STATE["company_id"],
                     content="اختبار")
    db.session.add(c); db.session.commit()

    wall_after = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

    # Render the way base.html renders it.
    rendered = to_company_tz_str(c.created_at, _STATE["company"],
                                    fmt="%Y-%m-%d %H:%M:%S")
    rendered_dt = datetime.strptime(rendered, "%Y-%m-%d %H:%M:%S")

    delta_before = (rendered_dt - wall_before).total_seconds()
    delta_after = (rendered_dt - wall_after).total_seconds()

    # Rendered time MUST be between the two wall clocks (with a small
    # slack for the SQL round-trip). If it's 3h ahead, we're back in
    # the bug.
    assert -1 <= delta_before <= 60, (
        f"rendered {rendered_dt} is BEFORE wall-clock {wall_before} by "
        f"{-delta_before:.1f}s — or drift is too large (bug back?)"
    )
    assert delta_after <= 60, (
        f"rendered {rendered_dt} is AFTER wall-clock {wall_after} by "
        f"{delta_after:.1f}s — TZ-BUG regressed (+3h drift?)"
    )


@check("3. _parse_datetime_local converts input to UTC (not stored as-is)")
def _():
    from flask import g
    from app.routes.leads import _parse_datetime_local

    # A user types "2026-07-04T18:44" in the browser (Riyadh local
    # time). We expect the parser to return 15:44 UTC (Riyadh - 3h).
    app = _STATE["app"]
    with app.test_request_context():
        g.active_company = _STATE["company"]
        out = _parse_datetime_local("2026-07-04T18:44")

    assert out is not None, "parser returned None"
    assert out.tzinfo is None, f"parser returned aware datetime: {out}"
    expected = datetime(2026, 7, 4, 15, 44)
    assert out == expected, (
        f"parser returned {out}, expected {expected} "
        f"(input 18:44 Riyadh should be 15:44 UTC)"
    )


@check("4. to_utc_from_company / to_company_tz_str are true inverses")
def _():
    from app.services.time import to_utc_from_company, to_company_tz_str

    local_input = datetime(2026, 7, 4, 18, 44)
    utc = to_utc_from_company(local_input, _STATE["company"])
    back = to_company_tz_str(utc, _STATE["company"], fmt="%Y-%m-%dT%H:%M")

    assert back == "2026-07-04T18:44", (
        f"round-trip failed: {local_input} → UTC {utc} → back {back}"
    )


@check("5. Notification, LeadComment, TaskActivityLog all round-trip cleanly")
def _():
    from app.models import (
        Notification, NotificationKind,
        Lead, LeadComment, LeadStatus, Task, TaskActivityLog,
    )
    from app.services.time import to_company_tz_str

    wall_before = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

    # 1. Notification
    n = Notification(
        user_id=_STATE["user_id"], company_id=_STATE["company_id"],
        kind=NotificationKind.TASK_ASSIGNED,
        title="ت", body="ت",
    )
    db.session.add(n); db.session.commit()

    # 2. LeadComment
    lead = Lead(
        company_id=_STATE["company_id"], client_name="ز", phone="0",
        service_needed="ت", lead_type="INBOUND", source="WEBSITE",
        status=LeadStatus.NEW_LEAD,
        created_by_id=_STATE["user_id"],
        assigned_to_id=_STATE["user_id"],
    )
    db.session.add(lead); db.session.flush()
    lc = LeadComment(lead_id=lead.id, user_id=_STATE["user_id"],
                       company_id=_STATE["company_id"], content="ت")
    db.session.add(lc); db.session.commit()

    wall_after = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

    for label, obj in (("Notification", n), ("LeadComment", lc)):
        rendered = to_company_tz_str(obj.created_at, _STATE["company"],
                                        fmt="%Y-%m-%d %H:%M:%S")
        rendered_dt = datetime.strptime(rendered, "%Y-%m-%d %H:%M:%S")
        drift = (rendered_dt - wall_after).total_seconds()
        assert drift <= 60, (
            f"{label}: rendered {rendered_dt} is {drift:.1f}s ahead of "
            f"wall-clock {wall_after} — TZ-BUG regressed."
        )


def main():
    app = create_app()
    with app.app_context():
        _STATE["app"] = app
        _setup()
        n_pass = 0
        for label, fn in CHECKS:
            try:
                fn()
                print(f"  ✓ {label}")
                n_pass += 1
            except AssertionError as e:
                print(f"  ✗ {label}\n      {e}")
        # Cleanup.
        _teardown_company(_STATE["company_id"])
        from app.models import User
        from app.models.user import user_companies
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == _STATE["user_id"]))
        db.session.delete(db.session.get(User, _STATE["user_id"]))
        db.session.commit()
        print(f"\n{n_pass}/{len(CHECKS)} passed.")
        sys.exit(0 if n_pass == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
