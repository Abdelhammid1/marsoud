#!/usr/bin/env python3
"""MARSOUD-MOBILE-TKT-01 (2026-08-18) — audit for the three new
JSON APIs (Leads / Meetings / Schedules).

Verifies:
  L1: GET /api/v1/my/leads/stages returns full LeadStatus enum.
  L2: GET /api/v1/my/leads scopes to me when I'm not a manager.
  L3: GET /api/v1/my/leads/<id> returns full detail + history +
      activities lists.
  L4: POST /api/v1/my/leads/<id>/status moves the status +
      creates a LeadStatusEvent.
  L5: POST /api/v1/my/leads/<id>/activities inserts a LeadActivity.
  M1: POST /api/v1/my/meetings creates a CalendarEvent when no
      lead_id supplied.
  M2: POST /api/v1/my/meetings with lead_id → LeadActivity
      (type=MEETING) attached to the lead.
  M3: GET /api/v1/my/meetings merges the two sources.
  S1: GET /api/v1/my/schedules scopes to the caller.
  B1: All routes require bearer (401 without token).
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__MOBILE_EXTRAS_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User
    from app.models.user import user_companies

    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                try:
                    db.session.execute(
                        t.delete().where(t.c.company_id.in_(ids)))
                except Exception:
                    db.session.rollback()
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        try:
            db.session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": cid})
        except Exception:
            db.session.rollback()
    db.session.commit()


def _setup():
    from app.models import (
        Company, User, Plan, Lead, LeadStatus, CalendarEvent,
    )
    from app.models.crm_expansion import (
        LeadActivity, LeadActivityType,
    )
    from app.models.user import user_companies
    from app.models.task_schedule import TaskSchedule
    from app.services.legal import get_terms_version
    from app.services.api_tokens import generate_token

    _teardown()
    tv = get_terms_version()
    now = datetime.utcnow()
    plan = Plan.query.filter_by(code="growth").first() \
           or Plan.query.filter_by(code="pro").first()

    def _mk_user(email, phone=None):
        u = User(email=email, full_name=email,
                 phone=phone, terms_version=tv,
                 terms_accepted_at=now)
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        return u

    # Sales rep user (only sees his own leads)
    rep = _mk_user(f"{CO_NAME.lower()}_rep@x.local")
    # Sales manager (leads.view_all)
    mgr = _mk_user(f"{CO_NAME.lower()}_mgr@x.local")

    co = Company(name=f"{CO_NAME}_A", base_currency="EGP",
                 plan_id=plan.id if plan else None,
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=30))
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=rep.id, company_id=co.id, role="sales_rep"))
    db.session.execute(user_companies.insert().values(
        user_id=mgr.id, company_id=co.id, role="sales_manager"))

    # One lead assigned to the rep, one to nobody the rep can see
    lead_mine = Lead(company_id=co.id,
                      client_name="Client Mine",
                      phone="0100",
                      service_needed="consulting",
                      assigned_to_id=rep.id,
                      created_by_id=rep.id,
                      status=LeadStatus.NEW_LEAD)
    lead_other = Lead(company_id=co.id,
                       client_name="Client Other",
                       phone="0200",
                       service_needed="consulting",
                       assigned_to_id=mgr.id,
                       created_by_id=mgr.id,
                       status=LeadStatus.CONTACTED)
    db.session.add_all([lead_mine, lead_other])
    db.session.flush()

    # One existing activity on my lead
    db.session.add(LeadActivity(
        company_id=co.id, lead_id=lead_mine.id,
        type=LeadActivityType.CALL,
        subject="Intro call",
        activity_date=now - timedelta(days=1),
        created_by_id=rep.id))

    # A meeting-type LeadActivity in the future
    db.session.add(LeadActivity(
        company_id=co.id, lead_id=lead_mine.id,
        type=LeadActivityType.MEETING,
        subject="Kickoff meeting",
        activity_date=now + timedelta(days=2),
        created_by_id=rep.id))

    # A CalendarEvent tomorrow (my creation)
    db.session.add(CalendarEvent(
        company_id=co.id, created_by_id=rep.id,
        title="Team standup",
        starts_at=now + timedelta(days=1),
        location="Zoom"))

    # A TaskSchedule assigned to me
    db.session.add(TaskSchedule(
        company_id=co.id,
        title="Weekly report",
        description="Send weekly progress",
        assigned_to_id=rep.id,
        created_by_id=rep.id,
        recurrence="DAILY",
        start_date=now.date(),
        active=True,
        generated_count=0))

    db.session.commit()

    # Bearer tokens
    rep_token, _ = generate_token(rep, "audit:mobile-rep")
    mgr_token, _ = generate_token(mgr, "audit:mobile-mgr")
    _STATE.update(dict(
        co=co, rep=rep, mgr=mgr,
        lead_mine=lead_mine, lead_other=lead_other,
        rep_token=rep_token, mgr_token=mgr_token,
    ))


def _api(method, url, *, token=None, body=None):
    from flask import g as flask_g
    if "_login_user" in flask_g:
        del flask_g._login_user
    app = _STATE["app"]
    c = app.test_client()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = dict(headers=headers)
    if body is not None:
        kwargs["json"] = body
    return c.open(
        url + (("?" if "?" not in url else "&") +
               f"company_id={_STATE['co'].id}")
              if token else url,
        method=method, **kwargs)


# ─── B. Bearer required ───────────────────────────────────────────────
@check("B1: all new routes require bearer (401 without)")
def B1():
    for url in ("/api/v1/my/leads", "/api/v1/my/leads/stages",
                "/api/v1/my/meetings", "/api/v1/my/schedules"):
        r = _api("GET", url)
        assert r.status_code == 401, (url, r.status_code)


# ─── L. Leads ─────────────────────────────────────────────────────────
@check("L1: /leads/stages returns full LeadStatus enum with labels")
def L1():
    r = _api("GET", "/api/v1/my/leads/stages",
              token=_STATE["rep_token"])
    assert r.status_code == 200, (r.status_code, r.data[:200])
    body = json.loads(r.data)
    codes = {s["code"] for s in body["stages"]}
    for expected in ("NEW_LEAD", "CONTACTED", "MEETING_SCHEDULED",
                      "NEGOTIATION", "PROPOSAL_SENT", "WON", "LOST",
                      "NO_RESPONSE"):
        assert expected in codes, f"missing {expected}"
    # Arabic label present
    new_lead = next(s for s in body["stages"]
                     if s["code"] == "NEW_LEAD")
    assert new_lead["label_ar"] == "عميل جديد", new_lead


@check("L2: /leads (as sales_rep) shows only my assigned leads")
def L2():
    r = _api("GET", "/api/v1/my/leads",
              token=_STATE["rep_token"])
    assert r.status_code == 200
    body = json.loads(r.data)
    names = {l["client_name"] for l in body["leads"]}
    assert "Client Mine" in names, names
    assert "Client Other" not in names, "leaked across users"


@check("L3: /leads/<id> returns full detail + activities + history")
def L3():
    lid = _STATE["lead_mine"].id
    r = _api("GET", f"/api/v1/my/leads/{lid}",
              token=_STATE["rep_token"])
    assert r.status_code == 200, (r.status_code, r.data[:200])
    body = json.loads(r.data)
    lead = body["lead"]
    assert lead["id"] == lid
    assert isinstance(lead.get("activities"), list)
    assert isinstance(lead.get("history"), list)
    # Existing "Intro call" activity should be present
    subjects = {a["subject"] for a in lead["activities"]}
    assert "Intro call" in subjects, subjects


@check("L4: POST /leads/<id>/status moves + writes LeadStatusEvent")
def L4():
    from app.models import LeadStatus, LeadStatusEvent
    lid = _STATE["lead_mine"].id
    before = LeadStatusEvent.query.filter_by(lead_id=lid).count()
    r = _api("POST", f"/api/v1/my/leads/{lid}/status",
              token=_STATE["rep_token"],
              body={"new_status": "CONTACTED",
                    "note": "First call done"})
    assert r.status_code == 200, (r.status_code, r.data[:200])
    db.session.expire_all()
    from app.models import Lead
    l = db.session.get(Lead, lid)
    assert l.status == LeadStatus.CONTACTED, l.status
    after = LeadStatusEvent.query.filter_by(lead_id=lid).count()
    assert after == before + 1, (before, after)


@check("L5: POST /leads/<id>/activities inserts a LeadActivity")
def L5():
    from app.models.crm_expansion import LeadActivity
    lid = _STATE["lead_mine"].id
    before = LeadActivity.query.filter_by(lead_id=lid).count()
    r = _api("POST", f"/api/v1/my/leads/{lid}/activities",
              token=_STATE["rep_token"],
              body={"type": "EMAIL",
                    "subject": "Proposal sent",
                    "body": "Sent by email"})
    assert r.status_code == 201, (r.status_code, r.data[:200])
    after = LeadActivity.query.filter_by(lead_id=lid).count()
    assert after == before + 1, (before, after)


# ─── M. Meetings ──────────────────────────────────────────────────────
@check("M1: POST /meetings (no lead_id) creates a CalendarEvent")
def M1():
    from app.models import CalendarEvent
    before = CalendarEvent.query.filter_by(
        company_id=_STATE["co"].id).count()
    r = _api("POST", "/api/v1/my/meetings",
              token=_STATE["rep_token"],
              body={
                  "title": "Sprint planning",
                  "starts_at": (datetime.utcnow()
                                + timedelta(days=3)).isoformat(),
                  "location": "Office",
              })
    assert r.status_code == 201, (r.status_code, r.data[:200])
    after = CalendarEvent.query.filter_by(
        company_id=_STATE["co"].id).count()
    assert after == before + 1


@check("M2: POST /meetings with lead_id → LeadActivity type=MEETING")
def M2():
    from app.models.crm_expansion import LeadActivity, LeadActivityType
    lid = _STATE["lead_mine"].id
    before = LeadActivity.query.filter_by(
        lead_id=lid, type=LeadActivityType.MEETING).count()
    r = _api("POST", "/api/v1/my/meetings",
              token=_STATE["rep_token"],
              body={
                  "title": "Client walkthrough",
                  "starts_at": (datetime.utcnow()
                                + timedelta(days=5)).isoformat(),
                  "lead_id": lid,
              })
    assert r.status_code == 201, (r.status_code, r.data[:200])
    after = LeadActivity.query.filter_by(
        lead_id=lid, type=LeadActivityType.MEETING).count()
    assert after == before + 1


@check("M3: GET /meetings merges CalendarEvent + LeadActivity meetings")
def M3():
    r = _api("GET", "/api/v1/my/meetings",
              token=_STATE["rep_token"])
    assert r.status_code == 200, r.status_code
    body = json.loads(r.data)
    sources = {m["source"] for m in body["meetings"]}
    assert "calendar_event" in sources, sources
    assert "lead_activity" in sources, sources


# ─── S. Schedules ─────────────────────────────────────────────────────
@check("S1: GET /schedules scopes to the caller")
def S1():
    r = _api("GET", "/api/v1/my/schedules",
              token=_STATE["rep_token"])
    assert r.status_code == 200, r.status_code
    body = json.loads(r.data)
    titles = {s["title"] for s in body["schedules"]}
    assert "Weekly report" in titles, titles


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
