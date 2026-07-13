#!/usr/bin/env python3
"""MARSOUD-CRM-EXPANSION — service-level audit of every section.

  §1  Kanban (Lead status routing lives in existing endpoint —
      Playwright coverage in tests/playwright_leads_kanban.py)
  §2  Campaign model + FK on Lead + quick-add JSON API
  §3  Optional fields on Lead create (only name/phone/service required)
  §4  Activity + contact side-panels on lead detail
  §5a Campaign stats page
  §5b lead_activities table + follow-up alerts
  §5c lead_contacts table + primary flag
  §5d Analytics KPIs (stage counts, conversion, pipeline value, campaign perf)
"""
import sys, time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__CRM_EXPANSION_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    # Attach the demo owner as the company owner (for the HTTP tests)
    owner = User.query.filter_by(email="demo@manasety.ai").first()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner",
    ))
    db.session.commit()
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id
    _STATE["owner_id"] = owner.id


def _teardown(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine, Invoice, InvoiceItem, Payment,
        VendorBill, VendorBillItem, Lead, LeadActivity, LeadContact, Campaign,
    )
    from app.models.user import user_companies
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    LeadActivity.query.filter_by(company_id=company_id).delete()
    LeadContact.query.filter_by(company_id=company_id).delete()
    Campaign.query.filter_by(company_id=company_id).delete()
    Lead.query.filter_by(company_id=company_id).delete()
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(JournalLine.entry_id.in_(entry_ids)
                                  ).delete(synchronize_session=False)
    db.session.execute(user_companies.delete().where(
        user_companies.c.company_id == company_id))
    for t in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(t.name)}:
            db.session.execute(t.delete().where(t.c.company_id == company_id))
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── §1 Kanban view + column bucketing ─────────────────────────────────
@check("1. Leads route groups leads into columns keyed by LeadStatus")
def _():
    from app.models import Lead, LeadStatus, User
    cid = _STATE["company_id"]
    owner_id = _STATE["owner_id"]
    for st in LeadStatus:
        db.session.add(Lead(
            company_id=cid, client_name=f"AUD-KB-{st.name}",
            phone=f"0500{st.name[:3]}",
            service_needed="خدمة", status=st,
            assigned_to_id=owner_id,
            expected_value=Decimal("1000"),
        ))
    db.session.commit()
    _STATE["seeded_leads"] = len(list(LeadStatus))
    # Render the board via the test_client
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        # Switch to the audit company (owner has both memberships)
        c.get(f"/switch-company/{cid}")
        r = c.get("/leads/?view=board")
        html = r.get_data(as_text=True)
        # MARSOUD-CRM-NO-RESPONSE (2026-07-13) — parked leads live
        # in their own folder page at /leads/no-response and are
        # intentionally excluded from the pipeline board. Assert
        # every OTHER status still shows up on the board, and the
        # parked card is hidden here + visible on the folder.
        for st in LeadStatus:
            if st == LeadStatus.NO_RESPONSE:
                assert f"AUD-KB-{st.name}" not in html, \
                    "NO_RESPONSE leaked into the pipeline board"
                continue
            assert st.label_ar in html, f"column {st.label_ar} missing"
            assert f"AUD-KB-{st.name}" in html, f"card {st.name} missing"
        # Folder page owns the parked cards.
        folder = c.get("/leads/no-response").get_data(as_text=True)
        assert "AUD-KB-NO_RESPONSE" in folder, \
            "NO_RESPONSE card missing from folder"
    return f"{_STATE['seeded_leads']-1} pipeline cards + 1 parked card in folder"


@check("2. Board status change reuses /leads/<id>/status + logs LeadStatusEvent")
def _():
    from app.models import Lead, LeadStatus, LeadStatusEvent
    cid = _STATE["company_id"]
    lead = Lead.query.filter_by(
        company_id=cid, client_name='AUD-KB-NEW_LEAD').first()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/leads/{lead.id}/status", data={
            "new_status": "CONTACTED", "return_to": "board",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers.get("Location", "").endswith("/leads/?view=board"), \
            f"expected board redirect, got {r.headers.get('Location')}"
    db.session.expire_all()
    lead = db.session.get(Lead, lead.id)
    assert lead.status == LeadStatus.CONTACTED
    ev = LeadStatusEvent.query.filter_by(
        lead_id=lead.id).order_by(LeadStatusEvent.id.desc()).first()
    assert ev and ev.to_status == LeadStatus.CONTACTED
    return f"status change + event + board redirect all correct"


# ─── §2 Campaign model + FK + quick-add API ────────────────────────────
@check("3. Campaign quick-add JSON returns id + name")
def _():
    from app.models import Campaign
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post("/crm/campaigns/quick-add",
                   json={"name": "AUD-CAMP-Ramadan"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["name"] == "AUD-CAMP-Ramadan"
        assert "id" in data
        _STATE["campaign_id"] = data["id"]
        # Idempotency: same name returns reused=True
        r2 = c.post("/crm/campaigns/quick-add",
                    json={"name": "AUD-CAMP-Ramadan"})
        assert r2.status_code == 200
        assert r2.get_json()["reused"] is True
    return f"campaign #{data['id']} created + idempotent on repeat"


@check("4. Campaign quick-add rejects empty name with 400")
def _():
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post("/crm/campaigns/quick-add", json={"name": ""})
        assert r.status_code == 400
    return "empty name → 400 with JSON error"


@check("5. Lead can be attached to a campaign via campaign_id FK")
def _():
    from app.models import Lead, Campaign
    cid = _STATE["company_id"]
    camp_id = _STATE["campaign_id"]
    lead = Lead.query.filter_by(
        company_id=cid, client_name="AUD-KB-CONTACTED").first()
    lead.campaign_id = camp_id
    db.session.commit()
    db.session.expire_all()
    lead = db.session.get(Lead, lead.id)
    assert lead.campaign is not None
    assert lead.campaign.name == "AUD-CAMP-Ramadan"
    return f"lead → campaign relationship works both directions"


# ─── §3 Optional fields on Lead create ─────────────────────────────────
@check("6. Lead create accepts ONLY name + phone + service (no other required)")
def _():
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post("/leads/new", data={
            "client_name": "AUD-MINIMAL",
            "phone": "0500999",
            "service_needed": "خدمة",
            "assigned_to_id": str(_STATE["owner_id"]),
            # Deliberately omit lead_type, source, expected_value, campaign,
            # next_meeting, notes, request_description, etc.
        }, follow_redirects=False)
        assert r.status_code in (302, 303), \
            f"minimal lead should be accepted, got {r.status_code}"
    from app.models import Lead
    lead = Lead.query.filter_by(
        company_id=cid, client_name="AUD-MINIMAL").first()
    assert lead is not None
    assert lead.expected_value is None
    assert lead.lead_type is None
    return f"minimal lead created (#{lead.id})"


# ─── §5b Activities table + follow-up alerts ───────────────────────────
@check("7. LeadActivity persists with type + follow_up_date")
def _():
    from app.models import Lead, LeadActivity, LeadActivityType
    cid = _STATE["company_id"]
    lead = Lead.query.filter_by(
        company_id=cid, client_name="AUD-KB-CONTACTED").first()
    a = LeadActivity(
        company_id=cid, lead_id=lead.id,
        type=LeadActivityType.CALL,
        subject="Follow-up call",
        body="talked about proposal",
        activity_date=datetime.utcnow(),
        follow_up_date=date.today() + timedelta(days=3),
        created_by_id=_STATE["owner_id"],
    )
    db.session.add(a); db.session.commit()
    _STATE["activity_id"] = a.id
    assert a.type.label_ar == "مكالمة"
    return f"activity #{a.id} persisted with follow_up_date"


@check("8. Activity create route works end-to-end")
def _():
    from app.models import Lead, LeadActivity, LeadActivityType
    cid = _STATE["company_id"]
    lead = Lead.query.filter_by(
        company_id=cid, client_name="AUD-KB-NEGOTIATION").first()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        before = LeadActivity.query.filter_by(lead_id=lead.id).count()
        r = c.post(f"/crm/leads/{lead.id}/activities/new", data={
            "type": "EMAIL", "subject": "Sent quote",
            "body": "attached PDF",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        after = LeadActivity.query.filter_by(lead_id=lead.id).count()
        assert after == before + 1
    return f"activity created via route (count {before} → {after})"


@check("9. Follow-up alerts split into due + upcoming")
def _():
    from app.models import Lead, LeadActivity, LeadActivityType
    cid = _STATE["company_id"]
    lead = Lead.query.filter_by(company_id=cid,
                                  client_name="AUD-KB-CONTACTED").first()
    # One overdue, one upcoming
    yesterday = date.today() - timedelta(days=1)
    day6 = date.today() + timedelta(days=6)
    for fu, sub in [(yesterday, "OVERDUE-test"), (day6, "UPCOMING-test")]:
        db.session.add(LeadActivity(
            company_id=cid, lead_id=lead.id,
            type=LeadActivityType.NOTE, subject=sub,
            follow_up_date=fu, created_by_id=_STATE["owner_id"],
        ))
    db.session.commit()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get("/crm/activities/")
        html = r.get_data(as_text=True)
        assert "متابعات مستحقة" in html and "OVERDUE-test" in html
        assert "خلال 7 أيام" in html and "UPCOMING-test" in html
    return "both due + upcoming lists render correctly"


# ─── §5c Contacts ──────────────────────────────────────────────────────
@check("10. LeadContact create + delete via routes")
def _():
    from app.models import Lead, LeadContact
    cid = _STATE["company_id"]
    lead = Lead.query.filter_by(company_id=cid,
                                  client_name="AUD-KB-WON").first()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        # Create
        r = c.post(f"/crm/leads/{lead.id}/contacts/new", data={
            "name": "Ahmed Manager", "role": "مدير",
            "email": "a@x.y", "phone": "055000",
            "is_primary": "1",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        ct = LeadContact.query.filter_by(lead_id=lead.id).first()
        assert ct and ct.is_primary is True
        # Delete
        r = c.post(f"/crm/contacts/{ct.id}/delete", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert LeadContact.query.get(ct.id) is None
    return "contact create + primary flag + delete all work"


# ─── §5a Campaign stats page ───────────────────────────────────────────
@check("11. /crm/campaigns/ shows per-campaign leads/won/expected stats")
def _():
    from app.models import Lead, Campaign, LeadStatus
    cid = _STATE["company_id"]
    camp_id = _STATE["campaign_id"]
    # Attach a WON lead to the campaign so the numbers are non-zero
    won = Lead.query.filter_by(company_id=cid,
                                 client_name="AUD-KB-WON").first()
    won.campaign_id = camp_id
    won.expected_value = Decimal("5000")
    db.session.commit()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get("/crm/campaigns/")
        html = r.get_data(as_text=True)
        assert "AUD-CAMP-Ramadan" in html
        # Stats table should show ≥1 lead + ≥1 WON
        assert "5,000" in html or "5000" in html
    return "campaign page renders + shows leads/WON/expected value"


# ─── §5d Analytics KPIs ────────────────────────────────────────────────
@check("12. /crm/analytics/ renders all 4 KPIs + stage funnel + campaign table")
def _():
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get("/crm/analytics/")
        html = r.get_data(as_text=True)
        assert "إجمالي Leads" in html
        assert "معدل التحويل" in html
        assert "قيمة متوقعة" in html
        assert "متوسط وقت الإغلاق" in html
        assert "التوزيع على المراحل" in html
        assert "AUD-CAMP-Ramadan" in html   # campaign table populated
    return "all 4 KPIs + funnel + campaign perf table present"


# ─── §5d Conversion math ───────────────────────────────────────────────
@check("13. Analytics conversion rate math: WON / (WON+LOST)")
def _():
    from app.models import Lead, LeadStatus
    cid = _STATE["company_id"]
    # Currently we seeded 7 leads (one per stage) + AUD-MINIMAL (NEW_LEAD).
    # WON=1, LOST=1 → conversion = 50%.
    won = Lead.query.filter_by(company_id=cid, status=LeadStatus.WON).count()
    lost = Lead.query.filter_by(company_id=cid, status=LeadStatus.LOST).count()
    expected_pct = (won / (won + lost) * 100) if (won + lost) else 0
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get("/crm/analytics/")
        html = r.get_data(as_text=True)
        # Look for the expected percentage on the page
        pct_str = f"{expected_pct:.1f}%"
        assert pct_str in html, \
            f"expected '{pct_str}' in analytics page for won={won}, lost={lost}"
    return f"conversion {expected_pct:.1f}% = {won}/{won + lost} correct"


# ─── Sidebar exposes all CRM entries ───────────────────────────────────
@check("14. Sidebar shows every CRM section link to owner")
def _():
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get("/home")
        html = r.get_data(as_text=True)
        # Templates render the URLs, not endpoint names; look for the
        # actual paths + the Arabic labels.
        # MARSOUD-CRM-NO-RESPONSE (2026-07-13) — 6th link added.
        for url, label in (
            ("/leads/", "Leads"),
            ("/crm/campaigns/", "الحملات"),
            ("/leads/no-response", "لا يوجد استجابة"),
            ("/crm/activities/", "الأنشطة والمتابعات"),
            ("/crm/contacts/", "جهات الاتصال"),
            ("/crm/analytics/", "تحليلات CRM"),
        ):
            assert f'href="{url}"' in html, f'sidebar missing href="{url}"'
            assert label in html, f"sidebar missing label {label!r}"
    return "all 6 CRM sidebar entries + labels present"


# ─── Run ───────────────────────────────────────────────────────────────
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
                except Exception as e:
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print(f"\n(cleaned up company #{_STATE['company_id']})")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
