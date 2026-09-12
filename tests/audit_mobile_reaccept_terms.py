#!/usr/bin/env python3
"""MARSOUD-MOBILE-REACCEPT-TERMS-01 (2026-09-12) — /api/v1/auth/
accept-terms flow.

Fixes a real Apple + Google reviewer blocker: today, when the
super-admin publishes new terms, /api/v1/auth/login refuses to
mint a bearer with 403 terms_acceptance_required, and the mobile
app has no path to actually accept the terms (the old code just
told users to "open the browser", which is neither compliant nor
useful during app review). This suite guards the new endpoint that
lets the app accept terms in-flow.

Checks:
  1. POST /api/v1/auth/legal returns the terms + privacy HTML +
     current version.
  2. POST /api/v1/auth/accept-terms with agreed=true + valid creds
     → 200 + token, and the user row's terms_version now matches
     the platform's current version.
  3. Same call with agreed=false / missing → 400 agreement_required
     (nothing changes on the user row).
  4. Same call with a WRONG password → 401 invalid_credentials
     (nothing changes; the failure counter bumps like login).
  5. Same call on a stale terms user → after accepting, /login
     returns 200 (no more terms_acceptance_required loop).
  6. record_consent runs — one row is added to user_consent_log
     with source='mobile_reaccept'.
"""
import os
import sys
from pathlib import Path
from datetime import datetime

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix, *, published_version="v2.0"):
    """Fresh company + owner with a STALE terms_version. The
    published_version is what /re-accept-terms will demand."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan, PlatformSetting
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    # Publish some legal so has_published_legal() returns True and
    # login enforces the version check.
    for k, v in (
        ("terms_content_html", "<p>test terms</p>"),
        ("privacy_content_html", "<p>test privacy</p>"),
        ("terms_version", published_version),
    ):
        row = PlatformSetting.query.filter_by(key=k).first()
        if row:
            row.value = v
        else:
            db.session.add(PlatformSetting(key=k, value=v))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    email = f"owner__{prefix.lower()}__@x.io"
    # Deliberately STALE terms_version so login refuses.
    owner = User(email=email,
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version="v1.0",
                 terms_accepted_at=datetime.utcnow())
    owner.set_password("Pw12345678!")
    from app.models.user import user_companies
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c.id, owner.id, email


@check("1. GET /api/v1/auth/legal returns current terms + privacy "
        "+ version")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        _boot("RTM1", published_version="v9.0")
        client = app.test_client()
        r = client.get("/api/v1/auth/legal")
        assert r.status_code == 200, r.status_code
        data = r.get_json()
        assert data["terms_version"] == "v9.0"
        assert "test terms" in data["terms_html"]
        assert "test privacy" in data["privacy_html"]
        return "content endpoint returns terms + privacy + v9.0"


@check("2. POST /accept-terms with agreed=true + valid creds → "
        "200 + token; user.terms_version updates to current")
def _():
    from app import create_app, db
    from app.models import User
    app = create_app()
    with app.app_context():
        cid, oid, email = _boot("RTM2", published_version="v9.0")
        client = app.test_client()
        r = client.post("/api/v1/auth/accept-terms", json={
            "email": email,
            "password": "Pw12345678!",
            "agreed": True,
            "device_name": "audit-device",
        })
        assert r.status_code == 200, (r.status_code, r.get_data())
        data = r.get_json()
        assert data.get("token")
        assert data["user"]["email"] == email
        db.session.expire_all()
        user = db.session.get(User, oid)
        assert user.terms_version == "v9.0"
        assert user.terms_accepted_at is not None
        return f"token minted, terms_version → v9.0"


@check("3. agreed=false → 400 agreement_required; user row unchanged")
def _():
    from app import create_app, db
    from app.models import User
    app = create_app()
    with app.app_context():
        cid, oid, email = _boot("RTM3", published_version="v9.0")
        db.session.expire_all()
        before = db.session.get(User, oid).terms_version
        client = app.test_client()
        r = client.post("/api/v1/auth/accept-terms", json={
            "email": email,
            "password": "Pw12345678!",
            "agreed": False,
        })
        assert r.status_code == 400, r.status_code
        body = r.get_json()
        assert body["error"] == "agreement_required" or \
               body["message"] == "agreement_required"
        db.session.expire_all()
        after = db.session.get(User, oid).terms_version
        assert before == after
        return "refused; terms_version untouched"


@check("4. wrong password → 401 invalid_credentials; row unchanged")
def _():
    from app import create_app, db
    from app.models import User
    app = create_app()
    with app.app_context():
        cid, oid, email = _boot("RTM4", published_version="v9.0")
        client = app.test_client()
        r = client.post("/api/v1/auth/accept-terms", json={
            "email": email,
            "password": "wrong",
            "agreed": True,
        })
        assert r.status_code == 401, r.status_code
        db.session.expire_all()
        assert db.session.get(User, oid).terms_version == "v1.0"
        return "wrong pw refused; terms untouched"


@check("5. after accepting, subsequent /login returns 200 (no more "
        "terms_acceptance_required)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, email = _boot("RTM5", published_version="v9.0")
        client = app.test_client()
        # First login → 403 terms_acceptance_required
        r1 = client.post("/api/v1/auth/login", json={
            "email": email, "password": "Pw12345678!",
        })
        assert r1.status_code == 403, r1.status_code
        assert (r1.get_json().get("error") or
                r1.get_json().get("message")) == "terms_acceptance_required"
        # Accept terms
        r2 = client.post("/api/v1/auth/accept-terms", json={
            "email": email, "password": "Pw12345678!",
            "agreed": True,
        })
        assert r2.status_code == 200, (r2.status_code, r2.get_data())
        # Now login → 200
        r3 = client.post("/api/v1/auth/login", json={
            "email": email, "password": "Pw12345678!",
        })
        assert r3.status_code == 200, (r3.status_code, r3.get_data())
        return "login clean after accept-terms"


@check("6. record_consent runs — one row in user_consent_log with "
        "source='mobile_reaccept'")
def _():
    from app import create_app, db
    from sqlalchemy import text
    app = create_app()
    with app.app_context():
        cid, oid, email = _boot("RTM6", published_version="v9.0")
        client = app.test_client()
        client.post("/api/v1/auth/accept-terms", json={
            "email": email, "password": "Pw12345678!",
            "agreed": True,
        })
        rows = db.session.execute(text(
            "SELECT source, document_version FROM consent_events "
            "WHERE user_id = :u ORDER BY id DESC LIMIT 5"),
            {"u": oid}).fetchall()
        mine = [r for r in rows if r.source == "mobile_reaccept"]
        assert len(mine) == 1, f"expected 1 log row, got {len(mine)}"
        assert mine[0].document_version == "v9.0"
        return "consent logged with source='mobile_reaccept'"


def main():
    from app import create_app
    _ = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
