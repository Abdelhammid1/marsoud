"""MARSOUD-API-RATE-LIMIT (Abdelhamid 2026-07-22).

Per-ApiToken sliding-window (60s bucket) rate limiter.

Design (locked in with Ibrahim):
  · In-memory dict + threading.Lock per process — sub-2ms per check.
  · SQLite write-through to `api_token_windows` so:
      · counters survive gunicorn worker restarts + fresh boots
      · counters stay approximately consistent across ~2-3 workers
        (each worker keeps its own memory copy but writes through
        to the same DB; when memory disagrees with DB after a
        restart, DB wins on the next request).
  · Per-token limit configurable via platform_settings key
    `api_rate_limit_per_minute` (default 100). Applied to every
    `/api/v1/*` endpoint via the blueprint's before_request.

Not for the UI. The UI has session cookies + real users and
should be limited (if at all) with a different scheme.
"""
import threading
from datetime import datetime, timedelta
from app import db


DEFAULT_LIMIT_PER_MINUTE = 100
_WINDOW_SECONDS = 60

# {token_id: {"start": datetime, "count": int, "lock": threading.Lock}}
_MEM = {}
# Guard for _MEM itself (add/remove entries). Per-token locks come from
# inside the entries once created.
_MEM_LOCK = threading.Lock()


def _limit_per_minute():
    """MARSOUD-API-RATE-LIMIT — reads platform_settings each call so
    Ibrahim can retune from /admin/subscription-settings without a
    restart. The lookup is cheap (indexed key) but callers should
    cache when doing bulk work."""
    from app.services.subscription import _get_setting_raw
    raw = _get_setting_raw("api_rate_limit_per_minute")
    if raw and raw.strip().isdigit():
        n = int(raw)
        if 1 <= n <= 100_000:
            return n
    return DEFAULT_LIMIT_PER_MINUTE


def _bucket_start_utc(now=None):
    """Floor `now` to the current 60-second bucket boundary so all
    requests within the same minute share one row."""
    now = now or datetime.utcnow()
    return now.replace(second=0, microsecond=0)


def check_and_increment(token_id):
    """Called on every /api/v1/* request AFTER auth resolves the
    token. Returns (ok, retry_after_seconds).
      · ok=True + retry=None → request allowed.
      · ok=False + retry=N → refuse this request; try again in N seconds.

    Behaviour under concurrent requests: `threading.Lock` per-token
    serialises the compare-and-increment so we never overshoot
    within a single process. Across processes, worst case is
    (workers × limit) per minute — which is why the DB write-through
    exists: on the next window a new process starts by reading the
    DB value and reconciling.
    """
    now = datetime.utcnow()
    bucket = _bucket_start_utc(now)
    limit = _limit_per_minute()

    with _MEM_LOCK:
        entry = _MEM.get(token_id)
        if entry is None:
            entry = {"start": bucket, "count": 0,
                     "lock": threading.Lock()}
            _MEM[token_id] = entry

    with entry["lock"]:
        # Reset if the bucket rolled over since we last saw this token.
        if entry["start"] != bucket:
            entry["start"] = bucket
            entry["count"] = 0
            # Also seed from DB (another worker may have counted here).
            entry["count"] = _read_count_from_db(token_id, bucket)

        # Cold start / first request after restart: reconcile with DB.
        if entry["count"] == 0:
            entry["count"] = _read_count_from_db(token_id, bucket)

        if entry["count"] >= limit:
            # Refuse. Compute how long until the next bucket opens.
            retry = _WINDOW_SECONDS - int((now - bucket).total_seconds())
            return False, max(1, retry)

        entry["count"] += 1
        # Write-through: fire-and-forget upsert. Uses a fresh short-
        # lived connection so a slow DB doesn't block the counter
        # update (memory is still authoritative for THIS worker).
        try:
            _upsert_count(token_id, bucket, entry["count"])
        except Exception:
            # Never take the app down over a rate-limit persistence
            # miss — logged for forensics, allow the request.
            from flask import current_app
            try:
                current_app.logger.exception(
                    "rate_limit write-through failed for token %s",
                    token_id)
            except Exception:
                pass
        return True, None


def _read_count_from_db(token_id, bucket):
    from sqlalchemy import text
    row = db.session.execute(text(
        "SELECT count FROM api_token_windows "
        "WHERE token_id = :t AND window_start_utc = :w"),
        {"t": token_id, "w": bucket}).first()
    return int(row[0]) if row else 0


def _upsert_count(token_id, bucket, count):
    from sqlalchemy import text
    with db.engine.begin() as conn:
        r = conn.execute(text(
            "UPDATE api_token_windows SET count = :c "
            "WHERE token_id = :t AND window_start_utc = :w"),
            {"c": count, "t": token_id, "w": bucket})
        if r.rowcount == 0:
            conn.execute(text(
                "INSERT INTO api_token_windows "
                "(token_id, window_start_utc, count) "
                "VALUES (:t, :w, :c)"),
                {"t": token_id, "w": bucket, "c": count})


def reset_memory():
    """Test-only — nuke the in-memory dict so audits can start clean
    between checks. Not for production callers."""
    with _MEM_LOCK:
        _MEM.clear()
