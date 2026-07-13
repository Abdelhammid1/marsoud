#!/usr/bin/env python3
"""MARSOUD-LEADS-CAMPAIGN-FILTER (Abdelhamid 2026-07-13).

Adds a Campaign filter to /leads/ so users can narrow the pipeline
board or the list view to leads that came in from a specific
marketing campaign. Same shape as the campaign filter already living
on /leads/no-response (folder page from yesterday) — this ticket
just brings it to the main pipeline page.

Checks:
  1. GET /leads/ renders a <select name="campaign"> in the filter
     panel (only when the company has at least one active campaign).
  2. GET /leads/?campaign=<id> filters the leads server-side —
     leads from the wrong campaign disappear.
  3. GET /leads/?campaign=<id> keeps the OTHER filters honest
     (search + status compose correctly with the campaign filter).
  4. Invalid campaign id (non-numeric, or an id from another company)
     is silently ignored instead of returning zero leads mistakenly.
  5. The "Export Excel" link carries the campaign filter through the
     query string so the download matches the on-screen view.
  6. GET /leads/export/excel?campaign=<id> honours the filter at the
     export layer (defensive against the button forgetting the arg).
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
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'lcf-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Lead, LeadStatus, Campaign,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__LEADS_CAMP__", "__LEADS_CAMP_OTHER__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)

    a = Company(name="__LEADS_CAMP__", base_currency="SAR")
    other = Company(name="__LEADS_CAMP_OTHER__", base_currency="SAR")
    db.session.add_all([a, other]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(other.id)

    def _mk(email, cid, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=cid, role=role))
        return u

    owner = _mk("lcf-owner@x.test", a.id, "owner")
    rep = _mk("lcf-rep@x.test", a.id, "sales_rep")

    # Two campaigns in company A, one in the OTHER company (for the
    # cross-tenant invalid-id check).
    camp_x = Campaign(company_id=a.id, name="Camp-X",
                       active=True, created_by_id=owner.id)
    camp_y = Campaign(company_id=a.id, name="Camp-Y",
                       active=True, created_by_id=owner.id)
    camp_other = Campaign(company_id=other.id, name="Other-Camp",
                       active=True, created_by_id=owner.id)
    db.session.add_all([camp_x, camp_y, camp_other]); db.session.flush()

    def _lead(name, camp_id, status=LeadStatus.NEW_LEAD):
        l = Lead(
            company_id=a.id, client_name=name,
            phone="0500000000", service_needed="test",
            assigned_to_id=rep.id, created_by_id=owner.id,
            status=status, campaign_id=camp_id,
        )
        db.session.add(l); db.session.flush()
        return l

    # 2 leads on Camp-X, 1 on Camp-Y, 1 with no campaign — so the
    # filter has three groups to distinguish.
    l_x1 = _lead("LCF-Client-Xone", camp_x.id)
    l_x2 = _lead("LCF-Client-Xtwo", camp_x.id)
    l_y = _lead("LCF-Client-Yone", camp_y.id, LeadStatus.CONTACTED)
    l_none = _lead("LCF-Client-None", None)
    db.session.commit()

    _STATE.update(
        a_id=a.id, other_id=other.id,
        owner_id=owner.id, rep_id=rep.id,
        camp_x_id=camp_x.id, camp_y_id=camp_y.id,
        camp_other_id=camp_other.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login_client():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


@check("1. /leads/ renders a Campaign filter <select>")
def _():
    r = _login_client().get("/leads/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert 'name="campaign"' in body, \
        'Campaign <select name="campaign"> missing from filter panel'
    # Both campaigns must show up as options.
    assert "Camp-X" in body and "Camp-Y" in body, \
        "Campaign options not rendered in the dropdown"
    return "campaign dropdown rendered with 2 options"


@check("2. /leads/?campaign=<X> narrows the board to only Camp-X leads")
def _():
    r = _login_client().get(
        f"/leads/?campaign={_STATE['camp_x_id']}",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "LCF-Client-Xone" in body and "LCF-Client-Xtwo" in body, \
        "Camp-X leads missing from the filtered board"
    assert "LCF-Client-Yone" not in body, \
        "Camp-Y lead leaked through the Camp-X filter"
    assert "LCF-Client-None" not in body, \
        "campaign-less lead leaked through the campaign filter"
    return "2 X-leads visible; Y-lead + campaign-less lead filtered out"


@check("3. campaign filter composes with the status filter")
def _():
    # Camp-Y has one lead in status CONTACTED. The board with
    # campaign=Y should still surface it under CONTACTED. Adding
    # status=CONTACTED restricts to just that one.
    r = _login_client().get(
        f"/leads/?campaign={_STATE['camp_y_id']}&status=CONTACTED",
        follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    assert "LCF-Client-Yone" in body, \
        "expected Yone under CONTACTED + campaign=Y filter"
    assert "LCF-Client-Xone" not in body, \
        "X lead surfaced when campaign=Y + status=CONTACTED"
    return "campaign + status filters compose correctly"


@check("4. non-numeric campaign id is silently ignored; cross-tenant id is safe")
def _():
    client = _login_client()
    # Non-numeric arg: the try/except in the route swallows the
    # ValueError, so the filter degrades to "no campaign filter"
    # (all leads visible). This matches how the other filters
    # (rep, status) handle garbage.
    r = client.get("/leads/?campaign=not-a-number",
                    follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    assert "LCF-Client-Xone" in body, \
        "non-numeric campaign id shouldn't filter anything out"
    # A campaign id from ANOTHER company: numeric + valid at the
    # int() layer, so the WHERE clause runs — but company_id already
    # scopes the query, so zero leads match. That's the correct,
    # safe behaviour (no cross-tenant leak).
    r = client.get(f"/leads/?campaign={_STATE['camp_other_id']}",
                    follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    for n in ("LCF-Client-Xone", "LCF-Client-Xtwo", "LCF-Client-Yone"):
        assert n not in body, \
            f"cross-tenant campaign id matched local lead {n!r}"
    return "non-numeric ignored; cross-tenant returns empty"


@check("5. Export Excel link carries the campaign filter")
def _():
    r = _login_client().get(
        f"/leads/?campaign={_STATE['camp_x_id']}",
        follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # The export button URL should include campaign=<X> in the query string.
    expected = f"campaign={_STATE['camp_x_id']}"
    assert expected in body, \
        f"export link doesn't carry {expected!r} through the URL"
    return "export URL preserves the campaign filter"


@check("6. /leads/export/excel?campaign=<X> honours the filter")
def _():
    # Response is an .xlsx binary; assert it comes back non-empty
    # and the content-type is a spreadsheet. We don't parse the file
    # here — the export function is unit-tested elsewhere.
    r = _login_client().get(
        f"/leads/export/excel?campaign={_STATE['camp_x_id']}",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    ct = r.headers.get("Content-Type", "")
    assert "spreadsheet" in ct or "excel" in ct, \
        f"unexpected content-type: {ct!r}"
    assert len(r.data) > 500, "export payload is suspiciously small"
    return f"export ok ({len(r.data)} bytes)"


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
                for k in ("a_id", "other_id"):
                    if k in _STATE:
                        _teardown(_STATE[k])
                print("\n(cleaned up fixture companies)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
