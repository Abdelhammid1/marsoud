#!/usr/bin/env python3
"""MARSOUD-AI-ACTION-FRAMEWORK-01 (2026-09-03) — Confirm-to-Execute
foundation.

Checks:
  1. Schema + models importable.
  2. HTTP propose test_echo happy path.
  3. Unknown action_type → 400; zero rows inserted.
  4. HTTP confirm happy path → 200 + EXECUTED.
  5. Double confirm → 409 (idempotency); still 1 row.
  6. Cross-tenant confirm → 404.
  7. Expired confirm → 410, row → EXPIRED.
  8. Stale payload → 409, row → STALE.
  9. Executor raise → 500, row → REJECTED, session stays clean.
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
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

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return owner.email, c.id, owner.id


def _client_as(app, cid, uid):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["active_company_id"] = cid
    return c


@check("1. schema present + models importable")
def _():
    from app import create_app, db
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        tabs = inspect(db.engine).get_table_names()
        assert "ai_action_intents" in tabs
        cols = {c["name"] for c in inspect(db.engine).get_columns(
            "ai_action_intents")}
        for want in ("payload_hash", "expires_at",
                     "confirmed_by_user_id", "result_json",
                     "reject_reason", "action_type"):
            assert want in cols, f"missing column: {want}"
        from app.models import (
            AiActionIntent, AiActionIntentStatus,
            DEFAULT_EXPIRY_MINUTES,
        )
        assert DEFAULT_EXPIRY_MINUTES == 15
        assert AiActionIntentStatus.PENDING.value == "PENDING"
        return f"table + {len(cols)} columns + enum OK"


@check("2. HTTP propose test_echo → 201 + card + ~15 min expiry")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA2")
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "test_echo",
            "payload": {"message": "مرحبا يا مساعد"},
        })
        assert r.status_code == 201, \
            f"got {r.status_code}: {r.get_data(as_text=True)[:200]}"
        body = r.get_json()
        assert "intent_id" in body
        assert body["action_type"] == "test_echo"
        assert 14 * 60 <= body["expires_in_seconds"] <= 15 * 60
        # Card is a list of {label, value} dicts.
        card = body["confirmation_card"]
        assert isinstance(card, list) and len(card) >= 2
        assert any(item["value"] == "مرحبا يا مساعد" for item in card)
        # DB row landed as PENDING.
        row = db.session.get(AiActionIntent, body["intent_id"])
        assert row is not None
        assert row.status.value == "PENDING"
        assert row.payload_hash and len(row.payload_hash) == 64
        return f"intent #{row.id} PENDING, card {len(card)} rows"


@check("3. unknown action_type → 400, zero rows")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA3")
        before = AiActionIntent.query.filter_by(company_id=cid).count()
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "no_such_thing",
            "payload": {"x": 1},
        })
        assert r.status_code == 400, r.status_code
        after = AiActionIntent.query.filter_by(company_id=cid).count()
        assert after == before, f"row leaked: {before}→{after}"
        return "400 + nothing persisted"


@check("4. confirm happy path → 200 + EXECUTED + result_json")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA4")
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "test_echo",
            "payload": {"message": "hi"},
        })
        iid = r.get_json()["intent_id"]
        r2 = c.post(f"/api/ai-actions/{iid}/confirm")
        assert r2.status_code == 200, \
            f"got {r2.status_code}: {r2.get_data(as_text=True)[:200]}"
        body = r2.get_json()
        assert body["status"] == "EXECUTED"
        assert body["result"] == {"echoed": "hi", "length": 2}
        row = db.session.get(AiActionIntent, iid)
        assert row.status.value == "EXECUTED"
        assert row.confirmed_by_user_id == oid
        assert row.executed_at is not None
        assert json.loads(row.result_json) == body["result"]
        return "EXECUTED + result_json persisted"


@check("5. double confirm → 409, still 1 row")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA5")
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "test_echo",
            "payload": {"message": "again"},
        })
        iid = r.get_json()["intent_id"]
        r1 = c.post(f"/api/ai-actions/{iid}/confirm")
        assert r1.status_code == 200
        r2 = c.post(f"/api/ai-actions/{iid}/confirm")
        assert r2.status_code == 409, \
            f"double-confirm got {r2.status_code}"
        cnt = AiActionIntent.query.filter_by(company_id=cid).count()
        assert cnt == 1, f"expected 1 row, got {cnt}"
        return "second confirm → 409, no duplicate rows"


@check("6. cross-tenant confirm → 404 (never 403)")
def _():
    """Service-layer check — flask.test_client's session_transaction
    doesn't isolate cleanly when two clients are nested inside the
    same app_context (both end up sharing cookies), so we exercise
    the guard directly at the service boundary. Same guarantee: an
    active_company_id that doesn't match the intent's company_id
    refuses with a 404 payload."""
    from app import create_app, db
    from app.models import AiActionIntent
    from app.services.ai_actions import propose, confirm
    app = create_app()
    with app.app_context():
        email_a, cid_a, oid_a = _boot("AIA6A")
        email_b, cid_b, oid_b = _boot("AIA6B")
        # Tenant A proposes.
        r = propose(action_type="test_echo",
                    payload={"message": "hidden"},
                    company_id=cid_a, user_id=oid_a)
        iid = r["intent_id"]
        # Tenant B confirms → refuse.
        result, status = confirm(
            intent_id=iid,
            actor_user_id=oid_b,
            active_company_id=cid_b,   # tenant B, NOT owner tenant
        )
        assert status == 404, f"cross-tenant leaked: {status}"
        assert "غير موجود" in result.get("error", "")
        # And the row survives untouched (PENDING).
        row = db.session.get(AiActionIntent, iid)
        assert row.status.value == "PENDING", \
            f"cross-tenant attempt mutated status: {row.status.value}"
        return "cross-tenant → 404 + row untouched"


@check("7. expired confirm → 410, row → EXPIRED")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA7")
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "test_echo",
            "payload": {"message": "old"},
        })
        iid = r.get_json()["intent_id"]
        # Force expiry into the past.
        row = db.session.get(AiActionIntent, iid)
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        r2 = c.post(f"/api/ai-actions/{iid}/confirm")
        assert r2.status_code == 410, r2.status_code
        db.session.refresh(row)
        assert row.status.value == "EXPIRED"
        return "410 + EXPIRED"


@check("8. stale payload → 409, row → STALE")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA8")
        c = _client_as(app, cid, oid)
        r = c.post("/api/ai-actions/propose", json={
            "action_type": "test_echo",
            "payload": {"message": "fresh at propose"},
        })
        iid = r.get_json()["intent_id"]
        # Simulate the server-side data drifting between propose
        # and confirm by tampering with the stored hash.
        row = db.session.get(AiActionIntent, iid)
        row.payload_hash = "0" * 64
        db.session.commit()
        r2 = c.post(f"/api/ai-actions/{iid}/confirm")
        assert r2.status_code == 409, r2.status_code
        body = r2.get_json()
        assert body.get("status") == "STALE", body
        db.session.refresh(row)
        assert row.status.value == "STALE"
        assert "اتغيرت" in (row.reject_reason or "")
        return "409 + STALE + reason set"


@check("9. executor raise → 500 + REJECTED; next confirm still works")
def _():
    from app import create_app, db
    from app.models import AiActionIntent
    from app.services.ai_actions import (
        register_action, ActionSpec, _ACTION_REGISTRY,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("AIA9")
        # Register a bomb action just for this check (unregistered
        # in the finally block so the global registry stays clean).
        def _boom_exec(payload, _cid, _uid):
            raise RuntimeError("intentional boom for audit")
        def _pass_validate(_p): pass
        def _pass_fp(_p, _cid): return "boom-fp"
        def _pass_desc(_p): return [("النوع", "قنبلة اختبار")]
        register_action("_boom_test", ActionSpec(
            validate=_pass_validate, fingerprint=_pass_fp,
            execute=_boom_exec, describe_ar=_pass_desc,
        ))
        try:
            c = _client_as(app, cid, oid)
            r = c.post("/api/ai-actions/propose", json={
                "action_type": "_boom_test",
                "payload": {"anything": 1},
            })
            iid = r.get_json()["intent_id"]
            r2 = c.post(f"/api/ai-actions/{iid}/confirm")
            assert r2.status_code == 500, r2.status_code
            row = db.session.get(AiActionIntent, iid)
            assert row.status.value == "REJECTED"
            assert "boom" in (row.reject_reason or "").lower()
            # Session must still be usable — try another propose+
            # confirm of test_echo.
            r3 = c.post("/api/ai-actions/propose", json={
                "action_type": "test_echo",
                "payload": {"message": "after boom"},
            })
            assert r3.status_code == 201
            iid2 = r3.get_json()["intent_id"]
            r4 = c.post(f"/api/ai-actions/{iid2}/confirm")
            assert r4.status_code == 200, \
                f"session polluted after boom: {r4.status_code}"
            return "500 + REJECTED; recovery OK"
        finally:
            _ACTION_REGISTRY.pop("_boom_test", None)


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
