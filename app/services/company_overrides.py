"""MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08) — service layer
for per-tenant feature grant/deny overrides.

Mirrors `app/services/feature_flags.py` structurally (60s TTL
cache + threading lock + `_invalidate_cache` on writer + audit
log). Extends it with a per-company key space and an expiry
concept.

Priority in `app/services/access.py::can_access`:
    platform FeatureFlag  >  DENY override  >  GRANT override
                          >  plan module     >  role permission

An expired override (`expires_at` in the past) is treated as
absent — the row survives for the audit trail but stops
influencing decisions the moment the clock passes it.

The cache is keyed by company_id; a super-admin write to one
company invalidates only that company's slot, not the whole
platform.
"""
import threading
import time
from datetime import datetime

from app import db
from app.models import CompanyFeatureOverride


# ─── Cache (mirror of feature_flags._cache pattern) ─────────────────
_CACHE_TTL_SECONDS = 60
_cache_lock = threading.Lock()
# MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — cache key widened from
# `feature_code` to `(scope, feature_code)` so a subitem row for
# 'inventory.count' can coexist with a module row for 'inventory'
# on the same company without one clobbering the other in the
# cache dict.
# {company_id: (loaded_at, {(scope, feature_code): (mode, expires_at)})}
_cache: dict = {}


def _load_company_if_stale(company_id):
    now = time.time()
    entry = _cache.get(company_id)
    if entry and now - entry[0] < _CACHE_TTL_SECONDS:
        return entry[1]
    with _cache_lock:
        entry = _cache.get(company_id)
        if entry and now - entry[0] < _CACHE_TTL_SECONDS:
            return entry[1]
        try:
            rows = CompanyFeatureOverride.query.filter_by(
                company_id=company_id).all()
            # Key by (scope, feature_code) so subitem + module rows
            # on the same feature name are addressable separately.
            data = {(r.scope, r.feature_code): (r.mode, r.expires_at)
                    for r in rows}
        except Exception:
            data = {}
        _cache[company_id] = (now, data)
        return data


def _invalidate_for_company(company_id):
    with _cache_lock:
        _cache.pop(company_id, None)


def _invalidate_all():
    with _cache_lock:
        _cache.clear()


# ─── Validation ────────────────────────────────────────────────────
def _validate_scope(scope):
    """MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — scope decides
    which catalogue we validate feature_code against."""
    if scope not in ("MODULE", "SUBITEM"):
        raise ValueError(
            f"نوع الاستثناء غير صحيح (MODULE|SUBITEM)")


def _validate_feature_code(feature_code, scope="MODULE"):
    """Raise ValueError if the code isn't a known feature. An
    override written for a typo'd code would sit in the DB doing
    nothing forever.

    MODULE  → validated against feature_registry.all_module_codes()
    SUBITEM → validated against plan_gating.ALL_SUB_ITEM_ENDPOINTS
             (the same catalogue the sidebar + super-admin plan
             editor render). Keeping the two source-of-truth
             lists literally the same guarantees the picker only
             ever offers rows the resolver can actually honour.
    """
    if not feature_code:
        raise ValueError("رمز الميزة مطلوب")
    if scope == "SUBITEM":
        from app.services.plan_gating import ALL_SUB_ITEM_ENDPOINTS
        if feature_code not in ALL_SUB_ITEM_ENDPOINTS:
            raise ValueError(
                f"عنصر غير معروف: {feature_code!r}")
        return
    # scope == "MODULE"
    from app.services.feature_registry import all_module_codes
    if feature_code not in all_module_codes():
        raise ValueError(
            f"رمز الميزة {feature_code!r} غير موجود في سجل المميزات")


def _validate_mode(mode):
    if mode not in ("GRANT", "DENY"):
        raise ValueError(f"نوع الاستثناء غير صحيح (GRANT|DENY)")


def _validate_reason(reason):
    if not reason or not str(reason).strip():
        raise ValueError(
            "السبب إجباري — الرجاء توضيح لماذا هذا الاستثناء موجود")


# ─── Readers ───────────────────────────────────────────────────────
def get_override(company_id, feature_code, scope="MODULE"):
    """Returns 'GRANT' | 'DENY' | None. Expired rows treated as None.

    This is the function `access._company_override` delegates to
    for MODULE scope; SUBITEM callers should use the thin
    `get_subitem_override` wrapper below so the intent reads
    cleanly at the call site.

    MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — scope keyword grew;
    the cache key is `(scope, feature_code)` so a subitem row and
    a module row on the same feature name are addressable
    separately. Called on every gated request → hot path →
    cache-backed."""
    if not company_id or not feature_code:
        return None
    data = _load_company_if_stale(company_id)
    entry = data.get((scope, feature_code))
    if entry is None:
        return None
    mode, expires_at = entry
    if expires_at is not None and expires_at <= datetime.utcnow():
        return None
    return mode


def get_subitem_override(company_id, subitem_endpoint):
    """MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — thin wrapper so
    every subitem-read call site reads `get_subitem_override(...)`
    (self-documenting) instead of a bare `get_override(..., scope=
    'SUBITEM')`. Same fail-safe (None on empty inputs)."""
    return get_override(company_id, subitem_endpoint, scope="SUBITEM")


def list_for_company(company_id):
    """All overrides on a company, active or expired. UI sorts +
    colors them; this is deliberately unfiltered."""
    return CompanyFeatureOverride.query.filter_by(
        company_id=company_id
    ).order_by(CompanyFeatureOverride.created_at.desc()).all()


def list_all(company_id=None, mode=None, status=None, scope=None):
    """Every override on the platform for /admin/overrides.
    status ∈ ('active', 'expired', 'all'); active is the default.
    MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — optional scope
    filter (None = both MODULE and SUBITEM)."""
    q = CompanyFeatureOverride.query
    if company_id:
        q = q.filter(CompanyFeatureOverride.company_id == company_id)
    if mode in ("GRANT", "DENY"):
        q = q.filter(CompanyFeatureOverride.mode == mode)
    if scope in ("MODULE", "SUBITEM"):
        q = q.filter(CompanyFeatureOverride.scope == scope)
    if status == "active" or status is None:
        # NULL expires_at counts as active; a past date does not.
        now = datetime.utcnow()
        q = q.filter(
            (CompanyFeatureOverride.expires_at.is_(None)) |
            (CompanyFeatureOverride.expires_at > now)
        )
    elif status == "expired":
        now = datetime.utcnow()
        q = q.filter(
            CompanyFeatureOverride.expires_at.isnot(None),
            CompanyFeatureOverride.expires_at <= now,
        )
    # 'all' → no filter
    return q.order_by(CompanyFeatureOverride.created_at.desc()).all()


# ─── Writer ────────────────────────────────────────────────────────
def upsert_override(company_id, feature_code, mode, reason, *,
                     expires_at=None, actor_id=None,
                     scope="MODULE"):
    """Insert-or-update a company override. Validates everything up
    front, then commits + invalidates cache + writes audit log.

    Returns the row.
    Raises ValueError on validation failure (empty reason, unknown
    feature code, bad mode, bad scope).

    MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — scope keyword grew.
    Uniqueness is `(company_id, scope, feature_code)` so a subitem
    row for 'inventory.count' can coexist with a module row for
    'inventory' on the same company without one overwriting the
    other. Audit `details` gains `scope=` so the audit log
    distinguishes the two without a new action code (the
    audit-viewer UI needs no change)."""
    _validate_scope(scope)
    _validate_feature_code(feature_code, scope)
    _validate_mode(mode)
    _validate_reason(reason)

    row = CompanyFeatureOverride.query.filter_by(
        company_id=company_id, scope=scope,
        feature_code=feature_code,
    ).first()
    was_new = row is None
    if was_new:
        row = CompanyFeatureOverride(
            company_id=company_id,
            scope=scope,
            feature_code=feature_code,
        )
        db.session.add(row)
    row.mode = mode
    row.reason = reason.strip()
    row.expires_at = expires_at
    if actor_id:
        row.created_by_id = actor_id
    db.session.commit()
    _invalidate_for_company(company_id)

    # Audit log — mirror feature_flags.set_module shape. Failure of
    # the audit line never rolls back the commit.
    try:
        from app.services.superadmin import log_platform_action
        exp_str = ""
        if expires_at:
            exp_str = f" until={expires_at.isoformat()}"
        log_platform_action(
            "override_" + mode.lower(),
            target_company_id=company_id,
            actor_id=actor_id,
            details=(f"scope={scope} feature={feature_code}"
                     f"{exp_str} reason={reason.strip()[:200]}"),
        )
    except Exception:
        pass
    return row


def revoke_override(override_id, actor_id=None):
    """Delete an override + write an audit line preserving the
    reason + scope (in details). Returns True if a row was
    removed."""
    row = db.session.get(CompanyFeatureOverride, override_id)
    if row is None:
        return False
    company_id = row.company_id
    snap = (f"scope={row.scope} feature={row.feature_code} "
            f"mode={row.mode} "
            f"was_reason={(row.reason or '')[:200]}")
    db.session.delete(row)
    db.session.commit()
    _invalidate_for_company(company_id)
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "override_revoke",
            target_company_id=company_id,
            actor_id=actor_id,
            details=snap,
        )
    except Exception:
        pass
    return True


# ─── Cron surface (deferred to T11 per ticket scoping) ──────────────
def expiring_overrides(days=3):
    """List overrides expiring in the next N days. Unused by this
    ticket's callers; T11's Ops & Health Center wires the daily
    super-admin nag off this function."""
    from datetime import timedelta
    now = datetime.utcnow()
    cutoff = now + timedelta(days=days)
    return CompanyFeatureOverride.query.filter(
        CompanyFeatureOverride.expires_at.isnot(None),
        CompanyFeatureOverride.expires_at > now,
        CompanyFeatureOverride.expires_at <= cutoff,
    ).order_by(CompanyFeatureOverride.expires_at.asc()).all()
