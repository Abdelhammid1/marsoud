"""MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — ops-health composers.

Five pure read-only functions the admin/ops_health route calls to
build the "operational vitals" page. No writes, no side-effects,
no caching — every call is a fresh snapshot.

Composers:
  system_vitals    → DB up + total rows + total bytes + now_utc
  errors_summary   → PlatformError counts in the last N hours
  cron_last_runs   → per-job latest PlatformCronRun
  db_stats         → per-table row counts + total bytes
  audit_tail       → latest N PlatformAuditLog rows

Each composer is O(n_tables) or O(1) — safe to call every 15s.
"""
import json
from datetime import datetime, timedelta
from sqlalchemy import func, text

from app import db
from app.models import (
    PlatformError, PlatformAuditLog, PlatformCronRun,
)


# ─── 1. System vitals ─────────────────────────────────────────────
def system_vitals():
    """{db_ok, db_bytes, page_size, page_count, total_rows, now_utc}"""
    now = datetime.utcnow()
    db_ok = False
    page_size = page_count = 0
    total_rows = 0
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            db_ok = True
            # SQLite specifics — safe to try; ignore on other DBs.
            try:
                page_size = int(conn.execute(text("PRAGMA page_size")).scalar() or 0)
                page_count = int(conn.execute(text("PRAGMA page_count")).scalar() or 0)
            except Exception:
                page_size = page_count = 0
            # Total-rows sweep. Cheap on SQLite.
            try:
                tables = [r[0] for r in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' "
                    "AND name NOT LIKE 'alembic%'"))]
                for t in tables:
                    total_rows += int(conn.execute(
                        text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0)
            except Exception:
                total_rows = -1
    except Exception:
        db_ok = False
    return {
        "db_ok": db_ok,
        "db_bytes": page_size * page_count,
        "page_size": page_size,
        "page_count": page_count,
        "total_rows": total_rows,
        "now_utc": now,
    }


# ─── 2. Errors summary ────────────────────────────────────────────
def errors_summary(hours=24):
    """Aggregate PlatformError rows over the last N hours.

    Returns dict:
      total       int
      by_route    [(route, count)] top 10 desc
      by_status   [(status_code, count)] all statuses
      newest      [PlatformError, ...] top 5
    """
    cutoff = datetime.utcnow() - timedelta(hours=int(hours))
    base = PlatformError.query.filter(PlatformError.created_at >= cutoff)
    total = base.count()

    by_route_rows = (db.session.query(
                          PlatformError.route,
                          func.count(PlatformError.id).label("n"))
                      .filter(PlatformError.created_at >= cutoff)
                      .group_by(PlatformError.route)
                      .order_by(func.count(PlatformError.id).desc())
                      .limit(10)
                      .all())
    by_route = [(r or "—", int(n)) for r, n in by_route_rows]

    by_status_rows = (db.session.query(
                           PlatformError.status_code,
                           func.count(PlatformError.id).label("n"))
                       .filter(PlatformError.created_at >= cutoff)
                       .group_by(PlatformError.status_code)
                       .order_by(func.count(PlatformError.id).desc())
                       .all())
    by_status = [(s if s is not None else 0, int(n))
                 for s, n in by_status_rows]

    newest = (base.order_by(PlatformError.created_at.desc())
                  .limit(5).all())

    return {
        "total": int(total),
        "by_route": by_route,
        "by_status": by_status,
        "newest": newest,
        "hours": int(hours),
    }


# ─── 3. Cron last runs ────────────────────────────────────────────
def cron_last_runs():
    """One dict per distinct job_name (the latest by started_at).

    Includes the meta row '__tick__' — the whole-tick liveness
    signal that survives a single-job failure.

    Uses max(started_at) rather than max(id) so out-of-order
    backfills (tests, imports) still surface the actual newest
    row — in prod started_at is monotonic and the two agree.
    """
    latest_starts = (db.session.query(
                            PlatformCronRun.job_name.label("jn"),
                            func.max(PlatformCronRun.started_at).label("mx"))
                        .group_by(PlatformCronRun.job_name)
                        .subquery())
    rows = (db.session.query(PlatformCronRun)
            .join(latest_starts,
                   (PlatformCronRun.job_name == latest_starts.c.jn)
                   & (PlatformCronRun.started_at == latest_starts.c.mx))
            .order_by(PlatformCronRun.started_at.desc())
            .all())
    # A (job_name, started_at) collision would produce duplicate
    # rows; dedupe by first-wins on job_name (order is desc so
    # the "latest" of any ties is preserved).
    seen = set()
    unique = []
    for r in rows:
        if r.job_name in seen:
            continue
        seen.add(r.job_name)
        unique.append(r)
    rows = unique

    out = []
    for r in rows:
        duration_ms = None
        if r.finished_at and r.started_at:
            delta = r.finished_at - r.started_at
            duration_ms = int(delta.total_seconds() * 1000)
        summary = None
        if r.summary_json:
            try:
                summary = json.loads(r.summary_json)
            except Exception:
                summary = r.summary_json
        out.append({
            "job_name": r.job_name,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_ms": duration_ms,
            "status": r.status,
            "summary": summary,
            "error_message": r.error_message,
        })
    return out


# ─── 4. DB stats ──────────────────────────────────────────────────
def db_stats():
    """{total_bytes, page_size, page_count, tables: [(name, rows)]}
    tables sorted desc by row count, top 20."""
    page_size = page_count = 0
    tables = []
    try:
        with db.engine.connect() as conn:
            try:
                page_size = int(conn.execute(text("PRAGMA page_size")).scalar() or 0)
                page_count = int(conn.execute(text("PRAGMA page_count")).scalar() or 0)
            except Exception:
                pass
            names = [r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE 'alembic%'"))]
            for name in names:
                try:
                    n = int(conn.execute(
                        text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
                except Exception:
                    n = -1
                tables.append((name, n))
    except Exception:
        pass
    tables.sort(key=lambda r: r[1], reverse=True)
    return {
        "total_bytes": page_size * page_count,
        "page_size": page_size,
        "page_count": page_count,
        "tables": tables[:20],
        "table_count": len(tables),
    }


# ─── 5. Audit tail ────────────────────────────────────────────────
def audit_tail(limit=20):
    return (PlatformAuditLog.query
            .order_by(PlatformAuditLog.created_at.desc())
            .limit(int(limit))
            .all())
