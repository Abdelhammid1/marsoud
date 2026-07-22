"""MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22).

Cheap runtime lookup + toggle for module-level kill switches.

The middleware in app/__init__.py calls `is_module_enabled` on every
authenticated request that hits a gated blueprint; the answer needs
to be sub-millisecond. So we cache the whole flag set in memory for
60 seconds — a stale answer for up to a minute after a super-admin
toggle is a fine trade-off for the DB traffic saved.

Cache invalidation:
  · timeout-based (60s ttl); OR
  · explicit `_invalidate_cache()` called from set_module so the
    admin who just clicked "disable" sees the result on the next
    request (their next click into the module returns the 503).
"""
import threading
import time
from datetime import datetime
from app import db


_CACHE_TTL_SECONDS = 60
_cache_lock = threading.Lock()
_cache = {"loaded_at": 0.0, "flags": {}}   # module_key -> (enabled, reason)


def _load_cache_if_stale():
    now = time.time()
    if now - _cache["loaded_at"] < _CACHE_TTL_SECONDS and _cache["flags"]:
        return
    with _cache_lock:
        # Re-check after acquiring lock.
        if now - _cache["loaded_at"] < _CACHE_TTL_SECONDS and _cache["flags"]:
            return
        try:
            from app.models import FeatureFlag
            rows = FeatureFlag.query.all()
            _cache["flags"] = {
                r.module_key: (bool(r.enabled), r.disabled_reason)
                for r in rows
            }
            _cache["loaded_at"] = now
        except Exception:
            # Table missing during first migration run → treat as
            # all-enabled so nothing 503s.
            _cache["flags"] = {}
            _cache["loaded_at"] = now


def _invalidate_cache():
    with _cache_lock:
        _cache["flags"] = {}
        _cache["loaded_at"] = 0.0


def is_module_enabled(module_key):
    """Absence of a row = enabled (opt-out default). Only an explicit
    `enabled=False` row disables a module."""
    _load_cache_if_stale()
    row = _cache["flags"].get(module_key)
    if row is None:
        return True
    return row[0]


def disabled_reason(module_key):
    """The Arabic text the super-admin typed when they killed the
    module — shown on the 503 page for context."""
    _load_cache_if_stale()
    row = _cache["flags"].get(module_key)
    if row is None:
        return None
    return row[1]


def set_module(module_key, enabled, reason, actor_id):
    """Upsert a flag row + log to PlatformAuditLog. Invalidates the
    cache so the next request picks up the new value immediately."""
    from app.models import FeatureFlag
    row = FeatureFlag.query.filter_by(module_key=module_key).first()
    if row is None:
        row = FeatureFlag(module_key=module_key)
        db.session.add(row)
    row.enabled = bool(enabled)
    row.disabled_reason = (reason or None) if not enabled else None
    row.updated_by_id = actor_id
    row.updated_at = datetime.utcnow()
    db.session.commit()
    _invalidate_cache()

    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "feature_flag_" + ("enable" if enabled else "disable"),
            actor_id=actor_id,
            details=f"module={module_key}"
                    + (f" reason={reason}" if reason else ""),
        )
    except Exception:
        pass
