"""MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — cron run tracker.

Context-manager wrapper for the try-blocks in app/routes/cron.py.
Writes a `platform_cron_runs` row on start (status='running'),
updates it on success or failure. Bookkeeping crashes NEVER
propagate — a broken tracker must not break the actual cron.

Usage in cron.py:

    with track_cron_job("marked_overdue") as ctx:
        n = 0
        for c in Company.query.filter_by(is_active=True).all():
            n += update_overdue_statuses(c.id)
        ctx.summary({"marked": n})   # optional

Retention: the successful trim keeps the last _KEEP_PER_JOB rows
per job_name and drops older ones. At the default cadence
(1 tick / 5 min => 288/day) this is ~3.5 days of history — enough
to catch "when did this last stop working?" without unbounded
growth.
"""
import json
from contextlib import contextmanager
from datetime import datetime

from app import db
from app.models import PlatformCronRun


_KEEP_PER_JOB = 1000


class _Ctx:
    """Handle the caller uses to attach a summary payload."""
    __slots__ = ("_summary",)

    def __init__(self):
        self._summary = None

    def summary(self, obj):
        """Attach a JSON-serializable payload to this run row.
        Whatever the job returns (int / dict / list / None) is
        fine — we json.dumps(default=str) it and cap at 2000 chars."""
        self._summary = obj


@contextmanager
def track_cron_job(job_name):
    row = PlatformCronRun(
        job_name=job_name,
        started_at=datetime.utcnow(),
        status="running",
    )
    ctx = _Ctx()

    # ─── Insert the "running" row. Bookkeeping failure => carry on. ─
    try:
        db.session.add(row)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        row = None

    # ─── Run the wrapped block ────────────────────────────────────
    try:
        yield ctx
    except Exception as e:
        if row is not None:
            _mark(row, status="error", ctx=ctx, error=str(e)[:500])
        raise
    else:
        if row is not None:
            _mark(row, status="ok", ctx=ctx, error=None)
            _trim_tail(job_name)


def _mark(row, *, status, ctx, error):
    try:
        row.status = status
        row.finished_at = datetime.utcnow()
        row.error_message = error
        if ctx._summary is not None:
            try:
                row.summary_json = json.dumps(
                    ctx._summary, default=str)[:2000]
            except Exception:
                row.summary_json = None
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _trim_tail(job_name):
    """Keep only the last _KEEP_PER_JOB rows per job_name. SQLite
    doesn't have DELETE ... LIMIT so we use a subquery."""
    try:
        keep_ids = db.select(PlatformCronRun.id).where(
            PlatformCronRun.job_name == job_name
        ).order_by(PlatformCronRun.started_at.desc()).limit(_KEEP_PER_JOB)
        (PlatformCronRun.query
         .filter_by(job_name=job_name)
         .filter(~PlatformCronRun.id.in_(keep_ids))
         .delete(synchronize_session=False))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
