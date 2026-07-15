#!/usr/bin/env python3
"""MARSOUD-CRM-STATUS-ACTIVITY-SPLIT (Abdelhamid 2026-07-15).

Separates Lead Status (pipeline milestone) from Activities (record of
work done). Every activity carries a per-type outcome and MAY suggest
a status change — never auto-applied.

Checks:
  1. New activity types (WhatsApp, Visit, File-sent, Quote-sent,
     Contract-signed) are all present in the enum with Arabic labels
     + icons.
  2. lead_activities.outcome column exists after migration.
  3. outcomes_for(CALL) returns the call-specific list.
  4. outcomes_for(NOTE) is empty (no dropdown).
  5. suggest_status: CALL + "تم الرد" → CONTACTED.
  6. suggest_status: MEETING + "تم الاجتماع" → MEETING_SCHEDULED.
  7. suggest_status: CONTRACT_SIGNED + "تم التوقيع" → WON.
  8. suggest_status returns None when outcome doesn't map (لم يرد
     → no suggestion).
  9. suggest_status returns None when the suggestion equals the
     current status (avoid no-op prompt).
 10. POST /crm/leads/<id>/activities/new with outcome=X saves the
     outcome + stores a status suggestion in the session when
     applicable.
 11. POST /leads/<id>/suggest-status/apply actually changes the
     lead status.
 12. POST /leads/<id>/suggest-status/dismiss clears the suggestion
     WITHOUT touching the status.
 13. Timeline: activity with outcome shows the outcome pill in the
     rendered detail page.
"""
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

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
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'aos-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Lead, LeadStatus,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__AOS__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__AOS__", base_currency="SAR")
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

    owner = _mk("aos-owner@x.test", "owner")
    lead = Lead(
        company_id=a.id, client_name="AOS Client",
        phone="0500000000", service_needed="test",
        assigned_to_id=owner.id, created_by_id=owner.id,
        status=LeadStatus.NEW_LEAD,
    )
    db.session.add(lead); db.session.commit()
    _STATE.update(a_id=a.id, owner_id=owner.id, lead_id=lead.id)


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


# ─── Enum expansion ──────────────────────────────────────────────
@check("1. New activity types present with Arabic labels + icons")
def _():
    from app.models import LeadActivityType
    names = {t.name for t in LeadActivityType}
    for expected in ("WHATSAPP", "VISIT", "FILE_SENT",
                      "QUOTE_SENT", "CONTRACT_SIGNED"):
        assert expected in names, f"missing {expected}"
    # Every type has a label + icon (no KeyError).
    for t in LeadActivityType:
        assert t.label_ar
        assert t.icon
    return f"{len(names)} activity types with labels + icons"


@check("2. lead_activities.outcome column exists after migration")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("lead_activities")}
    assert "outcome" in cols, "outcome column missing"
    return "outcome column present"


# ─── Service catalogue ───────────────────────────────────────────
@check("3. outcomes_for(CALL) returns the call-specific list")
def _():
    from app.services.activity_outcomes import outcomes_for
    from app.models import LeadActivityType
    outs = outcomes_for(LeadActivityType.CALL)
    assert "لم يرد" in outs, f"CALL outcomes: {outs}"
    assert "تم الرد" in outs
    return f"CALL has {len(outs)} outcomes"


@check("4. outcomes_for(NOTE) is empty (no dropdown for notes)")
def _():
    from app.services.activity_outcomes import outcomes_for
    from app.models import LeadActivityType
    outs = outcomes_for(LeadActivityType.NOTE)
    assert outs == (), f"NOTE shouldn't have outcomes, got {outs}"
    return "NOTE outcomes = ()"


# ─── Status suggestions ─────────────────────────────────────────
@check("5. CALL + 'تم الرد' suggests CONTACTED")
def _():
    from app.services.activity_outcomes import suggest_status
    from app.models import LeadActivityType, LeadStatus
    s = suggest_status(
        LeadActivityType.CALL, "تم الرد", LeadStatus.NEW_LEAD)
    assert s == LeadStatus.CONTACTED, f"got {s}"
    return "→ CONTACTED"


@check("6. MEETING + 'تم الاجتماع' suggests MEETING_SCHEDULED")
def _():
    from app.services.activity_outcomes import suggest_status
    from app.models import LeadActivityType, LeadStatus
    s = suggest_status(
        LeadActivityType.MEETING, "تم الاجتماع", LeadStatus.CONTACTED)
    assert s == LeadStatus.MEETING_SCHEDULED, f"got {s}"
    return "→ MEETING_SCHEDULED"


@check("7. CONTRACT_SIGNED + 'تم التوقيع' suggests WON")
def _():
    from app.services.activity_outcomes import suggest_status
    from app.models import LeadActivityType, LeadStatus
    s = suggest_status(
        LeadActivityType.CONTRACT_SIGNED, "تم التوقيع",
        LeadStatus.NEGOTIATION,
    )
    assert s == LeadStatus.WON, f"got {s}"
    return "→ WON"


@check("8. Outcome that doesn't map returns None")
def _():
    from app.services.activity_outcomes import suggest_status
    from app.models import LeadActivityType, LeadStatus
    s = suggest_status(
        LeadActivityType.CALL, "لم يرد", LeadStatus.NEW_LEAD)
    assert s is None, f"unexpected suggestion: {s}"
    return "no suggestion for 'لم يرد'"


@check("9. Suggestion equal to current status returns None (no no-op prompt)")
def _():
    from app.services.activity_outcomes import suggest_status
    from app.models import LeadActivityType, LeadStatus
    s = suggest_status(
        LeadActivityType.CALL, "تم الرد", LeadStatus.CONTACTED)
    assert s is None, f"should not suggest same status, got {s}"
    return "same status → no suggestion"


# ─── HTTP ─────────────────────────────────────────────────────────
@check("10. POST activity with outcome saves it + stores suggestion in session")
def _():
    from app.models import LeadActivity
    client = _login()
    r = client.post(
        f"/crm/leads/{_STATE['lead_id']}/activities/new",
        data={"type": "MEETING", "outcome": "تم الاجتماع",
              "subject": "kick-off"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    row = LeadActivity.query.filter_by(
        lead_id=_STATE["lead_id"]).order_by(
            LeadActivity.id.desc()).first()
    assert row is not None
    assert row.outcome == "تم الاجتماع"
    # Session should carry a suggestion for MEETING_SCHEDULED.
    with client.session_transaction() as sess:
        sug = sess.get("status_suggestion")
    assert sug is not None, "no session suggestion stored"
    assert sug["lead_id"] == _STATE["lead_id"]
    assert sug["suggested"] == "MEETING_SCHEDULED"
    _STATE["client"] = client
    return f"activity saved with outcome + session suggestion set"


@check("11. POST /suggest-status/apply flips the lead status")
def _():
    from app.models import Lead, LeadStatus
    client = _STATE["client"]
    r = client.post(
        f"/leads/{_STATE['lead_id']}/suggest-status/apply",
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    l = db.session.get(Lead, _STATE["lead_id"])
    assert l.status == LeadStatus.MEETING_SCHEDULED, \
        f"expected MEETING_SCHEDULED, got {l.status}"
    with client.session_transaction() as sess:
        assert "status_suggestion" not in sess, \
            "suggestion should be cleared after apply"
    return f"status → MEETING_SCHEDULED; session cleared"


@check("12. POST /suggest-status/dismiss clears without changing status")
def _():
    from app.models import Lead, LeadActivity, LeadStatus
    client = _login()
    # Log another activity that triggers a suggestion.
    client.post(
        f"/crm/leads/{_STATE['lead_id']}/activities/new",
        data={"type": "CONTRACT_SIGNED", "outcome": "تم التوقيع"},
        follow_redirects=False,
    )
    before = db.session.get(Lead, _STATE["lead_id"]).status
    r = client.post(
        f"/leads/{_STATE['lead_id']}/suggest-status/dismiss",
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    after = db.session.get(Lead, _STATE["lead_id"]).status
    assert after == before, f"status changed: {before} → {after}"
    with client.session_transaction() as sess:
        assert "status_suggestion" not in sess
    return f"status stayed {before.name}; suggestion cleared"


@check("13. Activity outcome pill renders in the lead detail timeline")
def _():
    r = _login().get(f"/leads/{_STATE['lead_id']}",
                       follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    # The outcome we saved in check 10 was "تم الاجتماع".
    assert "تم الاجتماع" in body, "outcome not surfaced on detail"
    assert "🎯" in body, "outcome pill emoji missing"
    return "outcome pill visible on detail"


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
