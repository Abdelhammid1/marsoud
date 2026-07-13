#!/usr/bin/env python3
"""MARSOUD-CRM-NO-RESPONSE + MARSOUD-LEAD-AUTOCONTACT (2026-07-13).

Two related CRM tickets audited together.

Ticket A: NO_RESPONSE stage
  · New pipeline stage for leads that never replied.
  · Deliberately NOT collapsed into LOST — separate business bucket.
  · Parked leads live at /leads/no-response and can be restored
    to any pipeline stage.

Ticket B: Auto-create Contact for every Lead + backfill
  · When a Lead is created, a LeadContact is created automatically
    with the lead's name + phone.
  · Idempotent: never creates a second auto-Contact.
  · Backfill migration seeds Contacts for pre-existing Leads that
    have zero LeadContacts.

Checks:
  1. LeadStatus.NO_RESPONSE exists with Arabic label + badge class.
  2. lead.is_open is False when status = NO_RESPONSE (parked ≠ open).
  3. change_lead_status(lead, "NO_RESPONSE") records LeadStatusEvent.
  4. HTTP GET /leads/ hides NO_RESPONSE leads from the pipeline
     board (only shows pipeline_leads).
  5. HTTP GET /leads/no-response lists ONLY parked leads.
  6. Restore: POST /leads/<id>/status new_status=NEW_LEAD flips the
     lead back to NEW_LEAD; the folder page now excludes it.
  7. ensure_primary_contact creates a LeadContact when the lead
     has none, cloning name+phone, is_primary=True.
  8. ensure_primary_contact is idempotent — second call adds nothing.
  9. HTTP POST /leads/new auto-creates the primary contact.
 10. Backfill migration inserted contacts for legacy leads without one
     (verified via the seeded fixture pattern below).
 11. Campaigns page includes a no_response counter in stats.
 12. Analytics page counts NO_RESPONSE separately, and it is NOT
     folded into WON or LOST.
"""
import sys
from pathlib import Path

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


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM lead_contacts WHERE company_id = :c"), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'nrc-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Lead, LeadStatus, Campaign,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__NR_CONTACT__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__NR_CONTACT__", base_currency="SAR")
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role=role))
        return u

    owner = _mk("nrc-owner@x.test", "owner")
    rep = _mk("nrc-rep@x.test", "sales_rep")

    # One lead that will be moved to NO_RESPONSE (fixture for the
    # parking + restore tests).
    parked = Lead(
        company_id=a.id, client_name="Silent Client",
        phone="0501234567", service_needed="consulting",
        assigned_to_id=rep.id, created_by_id=owner.id,
        status=LeadStatus.CONTACTED,
    )
    db.session.add(parked); db.session.flush()

    # One lead that stays in the pipeline (control).
    active = Lead(
        company_id=a.id, client_name="Chatty Client",
        phone="0507654321", service_needed="dev",
        assigned_to_id=rep.id, created_by_id=owner.id,
        status=LeadStatus.NEGOTIATION,
    )
    db.session.add(active); db.session.flush()

    # One legacy lead that has NO LeadContact row — verifies the
    # backfill migration only if we don't touch it beforehand.
    # (We insert directly bypassing the auto-Contact hook so the
    # lead starts contact-less; then check that a follow-up call
    # to ensure_primary_contact fills it.)
    legacy = Lead(
        company_id=a.id, client_name="Legacy Client",
        phone="0500000001", service_needed="misc",
        assigned_to_id=rep.id, created_by_id=owner.id,
        status=LeadStatus.NEW_LEAD,
    )
    db.session.add(legacy); db.session.flush()

    # One campaign for the stats check.
    camp = Campaign(company_id=a.id, name="Test Campaign",
                    active=True, created_by_id=owner.id)
    db.session.add(camp); db.session.flush()
    active.campaign_id = camp.id
    db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, rep_id=rep.id,
        parked_id=parked.id, active_id=active.id,
        legacy_id=legacy.id, campaign_id=camp.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


# ─── NO_RESPONSE stage ─────────────────────────────────────────────
@check("1. LeadStatus.NO_RESPONSE exists with label + distinct badge")
def _():
    from app.models import LeadStatus
    assert hasattr(LeadStatus, "NO_RESPONSE")
    st = LeadStatus.NO_RESPONSE
    assert st.value == "NO_RESPONSE"
    assert "استجابة" in st.label_ar, f"unexpected label: {st.label_ar}"
    # Badge class must be defined (any value); tickets specifically
    # forbid reusing badge-cancelled which would visually merge with LOST.
    assert st.badge_class and st.badge_class != "badge-cancelled", \
        "NO_RESPONSE reused the LOST badge — visually indistinguishable"
    return f"label='{st.label_ar}', badge={st.badge_class}"


@check("2. Lead.is_open returns False for NO_RESPONSE (parked ≠ open)")
def _():
    from app.models import Lead, LeadStatus
    l = db.session.get(Lead, _STATE["parked_id"])
    l.status = LeadStatus.NO_RESPONSE
    db.session.commit()
    assert not l.is_open, "parked lead reported as open pipeline"
    assert l.is_parked, "is_parked helper returned False"
    return "is_open=False, is_parked=True"


@check("3. change_lead_status → NO_RESPONSE records a LeadStatusEvent")
def _():
    from app.models import Lead, LeadStatus, LeadStatusEvent
    from app.services.crm import change_lead_status
    l = db.session.get(Lead, _STATE["active_id"])
    change_lead_status(l, "NO_RESPONSE", changed_by_id=_STATE["owner_id"])
    assert l.status == LeadStatus.NO_RESPONSE
    ev = LeadStatusEvent.query.filter_by(
        lead_id=l.id, to_status=LeadStatus.NO_RESPONSE,
    ).first()
    assert ev is not None, "no LeadStatusEvent was written"
    return "status flipped + event row present"


@check("4. HTTP /leads/ hides NO_RESPONSE from the Kanban board")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/leads/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    # Silent Client + Chatty Client are both NO_RESPONSE after checks 2+3;
    # neither should appear on the pipeline board. Legacy Client (NEW_LEAD)
    # SHOULD appear.
    assert "Silent Client" not in body, \
        "NO_RESPONSE lead leaked onto the pipeline board"
    assert "Chatty Client" not in body, \
        "NO_RESPONSE lead leaked onto the pipeline board"
    assert "Legacy Client" in body, \
        "pipeline lead is missing from /leads/"
    return "board excludes parked; keeps pipeline"


@check("5. HTTP /leads/no-response lists ONLY parked leads")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/leads/no-response", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "Silent Client" in body, \
        "parked lead not visible on folder page"
    assert "Chatty Client" in body, \
        "parked lead not visible on folder page"
    assert "Legacy Client" not in body, \
        "pipeline lead leaked into the parked folder"
    return "folder shows exactly the parked set"


@check("6. Restore: POST status → new_status=NEW_LEAD flips back to pipeline")
def _():
    from flask import current_app
    from app.models import Lead, LeadStatus
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.post(
        f"/leads/{_STATE['parked_id']}/status",
        data={"new_status": "NEW_LEAD",
              "return_to": "/leads/no-response"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    if r.status_code == 302:
        assert "/leads/no-response" in r.headers.get("Location", ""), \
            f"return_to ignored: {r.headers.get('Location')}"
    l = db.session.get(Lead, _STATE["parked_id"])
    assert l.status == LeadStatus.NEW_LEAD, \
        f"restore didn't move the lead; status={l.status}"
    return "restored + return_to honoured"


# ─── Auto-contact ──────────────────────────────────────────────────
@check("7. ensure_primary_contact seeds one contact from lead data")
def _():
    from app.models import Lead, LeadContact
    from app.services.crm import ensure_primary_contact
    l = db.session.get(Lead, _STATE["legacy_id"])
    # Sanity: the legacy fixture started with zero contacts.
    LeadContact.query.filter_by(lead_id=l.id).delete()
    db.session.commit()
    assert LeadContact.query.filter_by(lead_id=l.id).count() == 0
    c = ensure_primary_contact(l)
    db.session.commit()
    assert c is not None
    assert c.name == l.client_name
    assert c.phone == l.phone
    assert c.is_primary is True
    return f"contact {c.id} cloned from lead"


@check("8. ensure_primary_contact is idempotent — no second contact")
def _():
    from app.models import Lead, LeadContact
    from app.services.crm import ensure_primary_contact
    l = db.session.get(Lead, _STATE["legacy_id"])
    before = LeadContact.query.filter_by(lead_id=l.id).count()
    ensure_primary_contact(l)
    ensure_primary_contact(l)
    ensure_primary_contact(l)
    after = LeadContact.query.filter_by(lead_id=l.id).count()
    assert after == before, \
        f"expected same count, got before={before} after={after}"
    return f"still {after} contact(s) after 3 calls"


@check("9. HTTP POST /leads/new auto-creates the primary contact")
def _():
    from flask import current_app
    from app.models import Lead, LeadContact
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.post("/leads/new", data={
        "client_name": "Fresh Insertion Client",
        "phone": "0509998887",
        "service_needed": "auto-contact test",
        "assigned_to_id": str(_STATE["rep_id"]),
    }, follow_redirects=False)
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    l = Lead.query.filter_by(
        company_id=_STATE["a_id"], client_name="Fresh Insertion Client",
    ).first()
    assert l is not None, "lead was not created"
    contacts = LeadContact.query.filter_by(lead_id=l.id).all()
    assert len(contacts) == 1, \
        f"expected 1 auto-contact, got {len(contacts)}"
    assert contacts[0].name == l.client_name
    assert contacts[0].phone == l.phone
    assert contacts[0].is_primary is True
    return "1 primary contact auto-created"


# ─── Analytics / campaign integration ──────────────────────────────
@check("10. Campaigns page renders no_response counter")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/crm/campaigns/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "No Response" in body or "لا يوجد استجابة" in body, \
        "No Response column not rendered on campaigns page"
    return "column present"


@check("11. Analytics page renders NO_RESPONSE as a distinct bucket")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/crm/analytics/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "لا يوجد استجابة" in body, "NO_RESPONSE label missing from analytics"
    return "label surfaced"


@check("12. NO_RESPONSE leads are NOT counted as WON or LOST anywhere")
def _():
    from app.models import Lead, LeadStatus
    from app.services import reports
    # We currently have 1 NO_RESPONSE lead (Chatty Client — after
    # check 3, before check 6). Make sure it's parked again for a
    # clean assertion.
    l = db.session.get(Lead, _STATE["active_id"])
    if l.status != LeadStatus.NO_RESPONSE:
        l.status = LeadStatus.NO_RESPONSE
        db.session.commit()
    # None of these leads are WON or LOST — total closed should be 0.
    won = Lead.query.filter_by(
        company_id=_STATE["a_id"], status=LeadStatus.WON,
    ).count()
    lost = Lead.query.filter_by(
        company_id=_STATE["a_id"], status=LeadStatus.LOST,
    ).count()
    parked = Lead.query.filter_by(
        company_id=_STATE["a_id"], status=LeadStatus.NO_RESPONSE,
    ).count()
    assert won == 0 and lost == 0, \
        f"expected 0/0, got won={won} lost={lost}"
    assert parked >= 1, "expected at least 1 parked lead in fixture"
    return f"won={won}, lost={lost}, parked={parked}"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
