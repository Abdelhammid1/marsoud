#!/usr/bin/env python3
"""MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24).
MARSOUD-CONTACT-LEAD-01 (Abdelhamid 2026-09-03) — added checks
11-14 for the structured-fields + source whitelist + 2-min
idempotency behaviour.

Checks:
  1. Fail-closed: no CONTACT_FORM_TOKEN configured → 500 refuse.
  2. Empty token in header → 401.
  3. Wrong token → 401.
  4. Correct token + valid payload → 201 + Lead row in Manasty CRM
     with source="نموذج التواصل - الموقع" (default "website" source).
  5. Missing name → 400.
  6. Missing service → 400.
  7. Missing email AND phone → 400.
  8. Valid: name + email only (phone empty) → 201 + Lead persists
     with a placeholder phone.
  9. Invalid email format → 400.
  10. >5 requests / minute from the same IP → 429 on the 6th.
  11. New-shape payload (company_name/service_type/package/
      description/source=landing_form) → 201, structured fields
      land on the correct Lead columns.
  12. source=contact_page → Arabic "صفحة التواصل" label.
  13. Bad source ("xss") → 400.
  14. Idempotency: same (phone, description) within 2 min →
      200 with dedup:true, no second row.
"""
import os
import sys
import time
from datetime import datetime
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


TOKEN = "test-contact-form-token-01"


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CL_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'cl-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))
    # Reset the in-memory rate-limit dict.
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()


def _ensure_manasty():
    """Create a company with id set by MANASTY_COMPANY_ID + an owner.
    The public endpoint reads MANASTY_COMPANY_ID from config and
    writes Leads into that id. We use a test id that doesn't clash
    with any existing seeded row."""
    from app.models import Company, User, UserStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from flask import current_app

    # Force a test manasty id — any id that doesn't collide with a
    # real fixture company.
    manasty_id = 8888
    current_app.config["MANASTY_COMPANY_ID"] = manasty_id
    current_app.config["CONTACT_FORM_TOKEN"] = TOKEN
    # SUPPORT_INBOX_USER_ID intentionally left unset so we exercise
    # the owner-fallback path.

    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM companies WHERE id = :i"), {"i": manasty_id})
    c = Company(id=manasty_id, name="__CL_MANASTY__",
                 base_currency="EGP", subdomain="cl-manasty",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="cl-manasty-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="cl-manasty-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    _STATE["manasty_id"] = manasty_id
    _STATE["owner_id"] = u.id
    return c, u


@check("1. Fail-closed when CONTACT_FORM_TOKEN not configured → 500")
def _():
    from flask import current_app
    _teardown()
    _ensure_manasty()
    # Wipe the token — endpoint should refuse EVERY request.
    current_app.config["CONTACT_FORM_TOKEN"] = ""
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead",
                     json={"name": "x", "phone": "0100", "service": "y"},
                     headers={"X-Contact-Form-Token": "anything"})
    assert r.status_code == 500, f"got {r.status_code}"
    # Restore for subsequent tests.
    current_app.config["CONTACT_FORM_TOKEN"] = TOKEN
    return "empty token config → 500 fail-closed"


@check("2. Empty token header → 401")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead",
                     json={"name": "x", "phone": "01", "service": "y"},
                     headers={"X-Contact-Form-Token": ""})
    assert r.status_code == 401, f"got {r.status_code}"
    return "empty header → 401"


@check("3. Wrong token → 401")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead",
                     json={"name": "x", "phone": "01", "service": "y"},
                     headers={"X-Contact-Form-Token": "wrong-token"})
    assert r.status_code == 401, f"got {r.status_code}"
    return "wrong token → 401"


@check("4. Valid payload → 201 + Lead in Manasty with correct source")
def _():
    from flask import current_app
    from app.models import Lead
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-Ahmed",
        "email": "cl-ahmed@x.test",
        "phone": "01000000001",
        "service": "خدمة استشارية",
        "message": "أحتاج تقييم فني",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 201, f"got {r.status_code}: {r.get_data(as_text=True)}"
    data = r.get_json()
    assert data.get("ok") is True
    lead = Lead.query.filter_by(client_name="cl-Ahmed").first()
    assert lead, "Lead not created"
    assert lead.company_id == _STATE["manasty_id"], \
        f"wrong company: {lead.company_id}"
    assert lead.source == "نموذج التواصل - الموقع", \
        f"wrong source: {lead.source}"
    assert lead.email == "cl-ahmed@x.test"
    assert lead.phone == "01000000001"
    assert lead.assigned_to_id == _STATE["owner_id"], \
        f"wrong assignee: {lead.assigned_to_id}"
    return f"Lead #{lead.id} created in Manasty"


@check("5. Missing name → 400")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "phone": "01", "service": "y"},
        headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 400
    assert "name" in (r.get_json() or {}).get("error", "")
    return "missing name → 400"


@check("6. Missing service → 400")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "x", "phone": "01"},
        headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 400
    return "missing service → 400"


@check("7. Missing email AND phone → 400")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "x", "service": "y"},
        headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 400
    return "missing both → 400"


@check("8. Email only (no phone) → 201 with placeholder phone")
def _():
    from flask import current_app
    from app.models import Lead
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-EmailOnly",
        "email": "cl-emailonly@x.test",
        "service": "استشارة",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 201, f"got {r.status_code}"
    lead = Lead.query.filter_by(client_name="cl-EmailOnly").first()
    assert lead
    assert lead.email == "cl-emailonly@x.test"
    assert lead.phone == "لم يُقدَّم", \
        f"expected placeholder, got: {lead.phone!r}"
    return "email-only lead persisted with placeholder phone"


@check("9. Invalid email format → 400")
def _():
    from flask import current_app
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()   # clean rate-limit slate
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "x", "email": "not-an-email", "service": "y",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 400, f"got {r.status_code}"
    return "invalid email → 400"


@check("10. Rate limit: 6th request from same IP → 429")
def _():
    from flask import current_app
    # Reset the counter so the previous checks don't push us over.
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()
    client = current_app.test_client()
    hdr = {"X-Contact-Form-Token": TOKEN,
           "X-Forwarded-For": "10.99.0.1"}
    # 5 requests succeed within the window. MARSOUD-CONTACT-LEAD-01:
    # each request needs a distinct (phone, description) or the
    # dedup layer collapses requests 2-5 into 200 dedup hits — the
    # rate-limit test would then never actually stress the limiter.
    for i in range(5):
        payload = {
            "name": f"cl-RL-{i}",
            "phone": f"010000001{i:02d}",
            "service_type": "rate-check",
            "description": f"rate-limit probe #{i}",
            "source": "website",
        }
        r = client.post("/api/v1/public/contact-lead",
                         json=payload, headers=hdr)
        assert r.status_code == 201, \
            f"request #{i+1} failed: {r.status_code} " \
            f"{r.get_data(as_text=True)}"
    # 6th should be 429 regardless of payload.
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-RL-6", "phone": "01000000199",
        "service_type": "rate", "source": "website",
    }, headers=hdr)
    assert r.status_code == 429, \
        f"expected 429, got {r.status_code}"
    return "5 succeed, 6th → 429"


@check("11. New-shape payload (source=landing_form) → 201 with "
        "structured fields on Lead columns")
def _():
    from flask import current_app
    from app.models import Lead
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-Structured",
        "phone": "01000000200",
        "company_name": "شركة اختبار الشلبي",
        "service_type": "تطبيق جوال",
        "package": "Growth",
        "description": "أحتاج تطبيق للتوصيل داخل الرياض",
        "source": "landing_form",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 201, \
        f"got {r.status_code}: {r.get_data(as_text=True)}"
    lead = Lead.query.filter_by(client_name="cl-Structured").first()
    assert lead, "Lead not created"
    assert lead.service_needed == "تطبيق جوال"
    assert lead.request_description == "أحتاج تطبيق للتوصيل داخل الرياض"
    assert lead.source == "نموذج الصفحة الرئيسية", \
        f"wrong source: {lead.source!r}"
    # notes carries the two extras.
    assert "شركة اختبار الشلبي" in (lead.notes or ""), \
        f"company missing from notes: {lead.notes!r}"
    assert "Growth" in (lead.notes or ""), \
        f"package missing from notes: {lead.notes!r}"
    return f"Lead #{lead.id} carries all structured fields"


@check("12. source=contact_page → صفحة التواصل label")
def _():
    from flask import current_app
    from app.models import Lead
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-CtPage",
        "phone": "01000000201",
        "service_type": "استشارة",
        "description": "from contact page",
        "source": "contact_page",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 201, f"got {r.status_code}"
    lead = Lead.query.filter_by(client_name="cl-CtPage").first()
    assert lead and lead.source == "صفحة التواصل", \
        f"wrong source: {lead.source!r}"
    return "contact_page → صفحة التواصل"


@check("13. Bad source value → 400 (whitelist rejects arbitrary "
        "strings)")
def _():
    from flask import current_app
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()
    client = current_app.test_client()
    r = client.post("/api/v1/public/contact-lead", json={
        "name": "cl-BadSrc", "phone": "01000000202",
        "service_type": "y", "source": "xss_injection_attempt",
    }, headers={"X-Contact-Form-Token": TOKEN})
    assert r.status_code == 400, \
        f"expected 400, got {r.status_code}: {r.get_data(as_text=True)}"
    return "unknown source → 400"


@check("14. Idempotency: same (phone, description) within 2 min → "
        "dedup, no second row")
def _():
    from flask import current_app
    from app.models import Lead
    from app.routes import public as _pub
    _pub._contact_ip_history.clear()
    client = current_app.test_client()
    payload = {
        "name": "cl-Dupe",
        "phone": "01000000203",
        "service_type": "استشارة",
        "description": "double-click submit",
        "source": "landing_form",
    }
    hdr = {"X-Contact-Form-Token": TOKEN}
    # First submit: 201 + new row.
    r1 = client.post("/api/v1/public/contact-lead",
                      json=payload, headers=hdr)
    assert r1.status_code == 201, \
        f"first: got {r1.status_code}: {r1.get_data(as_text=True)}"
    lead_id_1 = r1.get_json()["lead_id"]
    # Immediate resubmit with identical payload: 200 + dedup:true +
    # same lead_id, and NO extra Lead row.
    r2 = client.post("/api/v1/public/contact-lead",
                      json=payload, headers=hdr)
    assert r2.status_code == 200, \
        f"dupe: expected 200, got {r2.status_code}"
    body2 = r2.get_json()
    assert body2.get("dedup") is True, f"missing dedup flag: {body2}"
    assert body2.get("lead_id") == lead_id_1, \
        f"dedup returned different lead_id: {body2}"
    # Confirm exactly one row for this phone.
    n = Lead.query.filter_by(phone="01000000203").count()
    assert n == 1, f"expected 1 row after dedup, got {n}"
    # Same phone but DIFFERENT description → NOT a dupe.
    r3 = client.post("/api/v1/public/contact-lead", json={
        **payload, "description": "a genuine second enquiry"},
        headers=hdr)
    assert r3.status_code == 201, \
        f"second-genuine: expected 201, got {r3.status_code}"
    assert r3.get_json()["lead_id"] != lead_id_1
    return f"lead #{lead_id_1} deduped once, distinct description accepted"


def _final_teardown():
    _teardown()


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
            _final_teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
