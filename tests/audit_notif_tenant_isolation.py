#!/usr/bin/env python3
"""MARSOUD-NOTIF-TENANT-FIX + MARSOUD-LEAD-NUM-RESYNC
(Abdelhamid 2026-07-15).

Abdelhamid: "I'm registered on another company but when they do
something a notification comes to me inside my main company. Also
their leads are numbered L-0105 while they've barely added any."

Two separate bugs, one audit:

1. Cross-tenant notification leak — the bell/dropdown/read endpoints
   filtered by user_id only, ignoring g.active_company.

2. Lead numbering self-heal — a NumberSequence row with a stale
   high `next_number` (imported/migrated) made new leads jump to
   L-0105. The EMPLOYEE self-heal from July 2026 was extended to
   also cover LEAD in this batch.

Checks:
  1. Notification created for Company B doesn't show in Company A's
     bell (index page HTML doesn't contain it).
  2. Same for the JSON /notifications/dropdown endpoint.
  3. The unread counter reflects the active company only.
  4. read-all only marks the active company's rows read; Company
     B's unread stay unread.
  5. Marking a Company B notification as read from Company A's UI
     returns 404 (defense against a hand-crafted POST).
  6. Fresh company: next_number("LEAD") returns L-0001 even when a
     stale NumberSequence row says 105 — as long as no L-* number
     is actually in use.
  7. Same self-heal never moves the counter UP (safety guarantee).
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, date

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
            "DELETE FROM users WHERE email LIKE 'nti-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Notification, NotificationKind,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__NTI_A__", "__NTI_B__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__NTI_A__", base_currency="SAR")
    b = Company(name="__NTI_B__", base_currency="SAR")
    db.session.add_all([a, b]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(b.id)

    # The user is a member of BOTH companies — the ticket scenario.
    u = User(
        email="nti-shared@x.test",
        password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
        full_name="Shared User",
    )
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=a.id, role="owner"))
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=b.id, role="owner"))

    # 2 notifications for this user — one in each company. Distinct
    # titles so we can look for them in the HTML/JSON.
    db.session.add(Notification(
        company_id=a.id, user_id=u.id,
        kind=NotificationKind.LEAD_STATUS_CHANGED.value,
        title="NTI-Notif-Company-A",
    ))
    db.session.add(Notification(
        company_id=b.id, user_id=u.id,
        kind=NotificationKind.LEAD_STATUS_CHANGED.value,
        title="NTI-Notif-Company-B",
    ))
    db.session.commit()

    _STATE.update(a_id=a.id, b_id=b.id, user_id=u.id)


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login_as_a():
    """Log in as the shared user with active_company = A."""
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


# ─── Notifications ────────────────────────────────────────────────
@check("1. /notifications/ from Company A only shows A's notifications")
def _():
    r = _login_as_a().get("/notifications/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "NTI-Notif-Company-A" in body, "A's notif missing"
    assert "NTI-Notif-Company-B" not in body, \
        "B's notif leaked into A's page"
    return "A visible, B hidden"


@check("2. /notifications/dropdown JSON scoped to active company")
def _():
    r = _login_as_a().get("/notifications/dropdown",
                            follow_redirects=False)
    assert r.status_code == 200
    data = r.get_json()
    titles = [item["title"] for item in data.get("items", [])]
    assert "NTI-Notif-Company-A" in titles
    assert "NTI-Notif-Company-B" not in titles, \
        "B leaked into A's dropdown"
    return "dropdown scoped"


@check("3. unread count reflects the active company only")
def _():
    r = _login_as_a().get("/notifications/dropdown",
                            follow_redirects=False)
    data = r.get_json()
    # Only one unread notif in A (the one we seeded). B's shouldn't
    # inflate the counter.
    assert data["unread"] == 1, f"expected unread=1, got {data['unread']}"
    return f"unread={data['unread']}"


@check("4. read-all from A only marks A's; B's stay unread")
def _():
    from app.models import Notification
    client = _login_as_a()
    r = client.post("/notifications/read-all",
                     follow_redirects=False,
                     headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    a_unread = Notification.query.filter_by(
        user_id=_STATE["user_id"], company_id=_STATE["a_id"],
        read_at=None,
    ).count()
    b_unread = Notification.query.filter_by(
        user_id=_STATE["user_id"], company_id=_STATE["b_id"],
        read_at=None,
    ).count()
    assert a_unread == 0, f"A still has unread: {a_unread}"
    assert b_unread == 1, \
        f"B unread was disturbed by A's read-all: {b_unread}"
    return "A=0 unread, B=1 unread (unchanged)"


@check("5. Marking a Company B notification via A's UI returns 404")
def _():
    from app.models import Notification
    b_notif = Notification.query.filter_by(
        user_id=_STATE["user_id"], company_id=_STATE["b_id"],
    ).first()
    assert b_notif is not None, "fixture missing"
    r = _login_as_a().post(
        f"/notifications/{b_notif.id}/read",
        follow_redirects=False,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 404, \
        f"cross-tenant read succeeded: got {r.status_code}"
    return "cross-tenant read blocked (404)"


# ─── Lead numbering self-heal ─────────────────────────────────────
@check("6. Stale LEAD sequence self-heals to L-0001 for a fresh company")
def _():
    from app.models import Company, NumberSequence, Lead
    # Fresh company; no leads created.
    from app.services.seed_coa import seed_default_coa
    fresh = Company(name="__NTI_FRESH__", base_currency="SAR")
    db.session.add(fresh); db.session.flush()
    seed_default_coa(fresh.id)
    # Simulate the stale-import scenario: seed a NumberSequence row
    # for LEAD at 105 for this brand-new company. No leads exist,
    # so the self-heal should shrink back to 1.
    db.session.add(NumberSequence(
        company_id=fresh.id, doc_type="LEAD", next_number=105,
    ))
    db.session.commit()
    from app.services.numbering import next_number
    n = next_number(fresh.id, "LEAD")
    assert n == "L-0001", f"expected L-0001, got {n!r}"
    _teardown(fresh.id)
    return f"stale 105 → healed → {n}"


@check("7. Numbering never moves the counter UP during self-heal")
def _():
    from app.models import Company, NumberSequence
    from app.services.numbering import next_number
    from app.services.seed_coa import seed_default_coa
    fresh = Company(name="__NTI_UP__", base_currency="SAR")
    db.session.add(fresh); db.session.flush()
    seed_default_coa(fresh.id)
    # Start at 1 — no self-heal should push higher.
    db.session.add(NumberSequence(
        company_id=fresh.id, doc_type="LEAD", next_number=1,
    ))
    db.session.commit()
    n = next_number(fresh.id, "LEAD")
    assert n == "L-0001", f"expected L-0001, got {n!r}"
    _teardown(fresh.id)
    return "seq at 1 stays at 1"


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
                for k in ("a_id", "b_id"):
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
