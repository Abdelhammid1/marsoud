#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — Ops & Health audit.

Ten checks covering the tracker + composers + route render.

  1. track_cron_job on success → row status='ok' + finished_at set
  2. track_cron_job on raise    → row status='error' + re-raised
  3. track_cron_job bookkeeping crash → body still runs
  4. _trim_tail keeps only _KEEP_PER_JOB latest rows
  5. system_vitals returns db_ok + db_bytes + total_rows + now_utc
  6. errors_summary respects the hours window
  7. errors_summary by_route sorted desc
  8. cron_last_runs returns one row per distinct job_name (latest)
  9. db_stats returns sorted tables + total_bytes matches product
 10. GET /admin/ops-health renders 200 with every card marker
     and the meta-refresh tag
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__T11_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── Fixture ───────────────────────────────────────────────────
def _setup():
    """Minimal seed: one super-admin, one company/plan for the
    HTTP smoke check. No CoA / employees / anything domain — this
    audit is about platform-level composers only."""
    _teardown()
    from app.models import Company, Plan, User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__t11__").first()
    if not plan:
        plan = Plan(code="__t11__", name="T11", name_ar="T11",
                    allowed_subitems=None)
        plan.set_modules(["accounting"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                 subdomain="t11",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=(
                     datetime.utcnow() + timedelta(days=365)),
                 intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()

    sa = User(
        email=f"{PREFIX}sa@x.test", full_name="super admin",
        is_active=True, is_superadmin=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(sa); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=sa.id, company_id=c.id, role="owner"))
    db.session.commit()

    _STATE.update(company_id=c.id, plan_id=plan.id,
                   superadmin_id=sa.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Wipe our fixture's tracker rows and error rows.
        conn.execute(text(
            "DELETE FROM platform_cron_runs WHERE job_name LIKE '__t11_%'"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__T11_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM platform_errors WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__T11_%@x.test'"))
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code = '__t11__'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__t11__'"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. track_cron_job on success -> status='ok' + finished_at")
def _():
    from app.services.cron_tracking import track_cron_job
    from app.models import PlatformCronRun
    _setup()
    name = "__t11_ok"
    with track_cron_job(name) as ctx:
        ctx.summary({"n": 5})
    row = (PlatformCronRun.query
           .filter_by(job_name=name)
           .order_by(PlatformCronRun.id.desc()).first())
    assert row is not None, "no row written"
    assert row.status == "ok", row.status
    assert row.finished_at is not None
    assert row.finished_at >= row.started_at
    assert row.summary_json is not None
    assert '"n": 5' in row.summary_json


@check("2. track_cron_job on raise -> status='error' + re-raised")
def _():
    from app.services.cron_tracking import track_cron_job
    from app.models import PlatformCronRun
    _setup()
    name = "__t11_err"
    got = None
    try:
        with track_cron_job(name):
            raise ValueError("intentional boom")
    except ValueError as e:
        got = e
    assert got is not None, "exception was swallowed"
    row = (PlatformCronRun.query
           .filter_by(job_name=name)
           .order_by(PlatformCronRun.id.desc()).first())
    assert row is not None
    assert row.status == "error", row.status
    assert "intentional boom" in (row.error_message or "")


@check("3. track_cron_job bookkeeping crash keeps the body running")
def _():
    from unittest.mock import patch
    from app.services.cron_tracking import track_cron_job
    _setup()
    ran = {"body": False}

    # First commit (insert of 'running' row) blows up; the tracker
    # must swallow it and still yield to the body.
    original_commit = db.session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down for a moment")
        return original_commit()

    with patch.object(db.session, "commit", side_effect=flaky_commit):
        with track_cron_job("__t11_bookkeep_fail"):
            ran["body"] = True

    assert ran["body"] is True, "body did not run"


@check("4. _trim_tail keeps only _KEEP_PER_JOB latest rows")
def _():
    from app.services.cron_tracking import _trim_tail, _KEEP_PER_JOB
    from app.models import PlatformCronRun
    _setup()
    name = "__t11_trim"
    # Seed _KEEP_PER_JOB + 5 rows for this job_name.
    base = datetime.utcnow() - timedelta(hours=2)
    for i in range(_KEEP_PER_JOB + 5):
        db.session.add(PlatformCronRun(
            job_name=name,
            started_at=base + timedelta(seconds=i),
            finished_at=base + timedelta(seconds=i),
            status="ok"))
    db.session.commit()

    _trim_tail(name)

    kept = PlatformCronRun.query.filter_by(job_name=name).count()
    assert kept == _KEEP_PER_JOB, f"kept {kept} rows, expected {_KEEP_PER_JOB}"

    # The kept ones should be the newest → min started_at > base + 5s.
    newest_kept = PlatformCronRun.query.filter_by(
        job_name=name).order_by(PlatformCronRun.started_at.asc()).first()
    assert newest_kept.started_at >= base + timedelta(seconds=5), \
        "the trim kept old rows instead of new ones"


@check("5. system_vitals returns db_ok / db_bytes / total_rows / now_utc")
def _():
    from app.services.ops_health import system_vitals
    _setup()
    v = system_vitals()
    assert v["db_ok"] is True
    assert v["db_bytes"] > 0, v
    assert v["total_rows"] > 0, v
    assert isinstance(v["now_utc"], datetime)


@check("6. errors_summary respects the hours window")
def _():
    from app.services.ops_health import errors_summary
    from app.models import PlatformError
    _setup()
    now = datetime.utcnow()
    # 3 rows in-window, 2 rows out-of-window (48h old).
    for i in range(3):
        db.session.add(PlatformError(
            company_id=_STATE["company_id"],
            route=f"/in/{i}", method="GET", status_code=500,
            message="in",
            created_at=now - timedelta(minutes=i * 10)))
    for i in range(2):
        db.session.add(PlatformError(
            company_id=_STATE["company_id"],
            route=f"/out/{i}", method="GET", status_code=500,
            message="out",
            created_at=now - timedelta(hours=48 + i)))
    db.session.commit()

    s = errors_summary(hours=24)
    # Fixture might see rows from prior runs; assert AT LEAST 3.
    assert s["total"] >= 3, s["total"]
    # All out-of-window rows absent from newest / by_route.
    routes = {r for r, _ in s["by_route"]}
    assert "/out/0" not in routes
    assert "/out/1" not in routes


@check("7. errors_summary by_route sorted desc")
def _():
    from app.services.ops_health import errors_summary
    from app.models import PlatformError
    _setup()
    now = datetime.utcnow()
    # 5x /hot, 2x /cold, 1x /coldest
    for _ in range(5):
        db.session.add(PlatformError(
            company_id=_STATE["company_id"],
            route="/hot", method="GET", status_code=500,
            message="x", created_at=now))
    for _ in range(2):
        db.session.add(PlatformError(
            company_id=_STATE["company_id"],
            route="/cold", method="GET", status_code=500,
            message="x", created_at=now))
    db.session.add(PlatformError(
        company_id=_STATE["company_id"],
        route="/coldest", method="GET", status_code=500,
        message="x", created_at=now))
    db.session.commit()

    s = errors_summary(hours=1)
    routes = [r for r, _ in s["by_route"]]
    assert routes[0] == "/hot", routes
    # by_route counts strictly non-increasing.
    for a, b in zip(s["by_route"], s["by_route"][1:]):
        assert a[1] >= b[1], f"unsorted: {s['by_route']}"


@check("8. cron_last_runs returns one row per distinct job_name")
def _():
    from app.services.ops_health import cron_last_runs
    from app.models import PlatformCronRun
    _setup()
    now = datetime.utcnow()
    # 3 rows for A, 1 for B — cron_last_runs must return the LATEST
    # of A + the one B, and nothing else with these names.
    for i in range(3):
        db.session.add(PlatformCronRun(
            job_name="__t11_a",
            started_at=now - timedelta(minutes=i),
            finished_at=now - timedelta(minutes=i),
            status="ok"))
    db.session.add(PlatformCronRun(
        job_name="__t11_b",
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=5),
        status="ok"))
    db.session.commit()

    rows = cron_last_runs()
    ours = [r for r in rows if r["job_name"].startswith("__t11_")]
    names = {r["job_name"] for r in ours}
    assert names == {"__t11_a", "__t11_b"}, f"got names={sorted(names)!r}"
    # Latest __t11_a is the one whose started_at is `now` (i=0).
    a_row = next(r for r in ours if r["job_name"] == "__t11_a")
    delta = (now - a_row["started_at"]).total_seconds()
    assert -1 <= delta <= 60, \
        f"latest __t11_a is {delta}s away from `now` (expected the i=0 row)"


@check("9. db_stats returns sorted tables + total_bytes = page_size * page_count")
def _():
    from app.services.ops_health import db_stats
    _setup()
    s = db_stats()
    assert s["total_bytes"] == s["page_size"] * s["page_count"]
    assert s["tables"], "no tables reported"
    # Sorted desc.
    for a, b in zip(s["tables"], s["tables"][1:]):
        assert a[1] >= b[1], f"unsorted: {s['tables'][:5]}"


@check("10. GET /admin/ops-health renders 200 with every marker + meta-refresh")
def _():
    _setup()
    app = _STATE["app"]
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(_STATE["superadmin_id"])
        s["_fresh"] = True

    r = client.get("/admin/ops-health")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    for marker in ("🩺", "🛑", "⏰", "💾", "📜"):
        assert marker in body, f"marker {marker!r} missing"
    assert 'http-equiv="refresh"' in body, "meta-refresh missing"


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    with app.app_context():
        for label, fn in CHECKS:
            try:
                fn()
                passed += 1
                _p(f"  [OK] {label}")
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e)))
                _p(f"  [FAIL] {label}: {e}")
            except Exception as e:
                failed += 1
                failures.append((label, f"{type(e).__name__}: {e}"))
                _p(f"  [ERROR] {label}: {type(e).__name__}: {e}")
        _teardown()
    _p("")
    _p(f"audit_ops_health: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
