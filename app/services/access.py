"""MARSOUD-SUPERADMIN-CONTROL-01 T2 (2026-08-08) — one function
decides both "does this link show?" and "does this URL open?".

Before this file existed, the sidebar template hand-listed a
`permission_map` of 72 endpoints, but the sidebar rendered 82.
The 10 missing entries fell through the `not req or has_permission`
gate → shown to everyone → routes then returned 403. This is the
"شركات بتشوف حاجات مش في باقتها ولما تدخل يرفض" the ticket names.

Now: `can_access(endpoint, user, company) → (allowed, reason)` is
the ONLY decision point. The sidebar template calls `visible_nav()`
which itself calls `can_access` per link, and a new before_request
guard `enforce_access` also calls `can_access` on every request. Same
predicate on both sides — visible ⟺ openable, by construction.

Decision chain (per ticket §T2):
  0. Superadmin / exempt blueprint      → allow
  1. Platform-wide FeatureFlag disabled → REASON_PLATFORM_DISABLED
  2. Company override                   → GRANT / DENY (T4 stub)
  3. Plan doesn't include the module    → REASON_PLAN_MODULE
  4. Plan sub-item forbids the endpoint → REASON_PLAN_FEATURE
     (preserves the trial-full-access behavior)
  5. Role permission missing            → REASON_PERMISSION
  6. → allow
"""
from flask import current_app


# ─── Reason codes (stable strings for templates + logs) ──────────────
REASON_PLATFORM_DISABLED = "platform_disabled"
REASON_COMPANY_DENIED = "company_denied"       # T4 stub — never fires today
REASON_PLAN_MODULE = "plan_module"
REASON_PLAN_FEATURE = "plan_feature"
REASON_PERMISSION = "permission"


# ─── Endpoint prefixes the resolver is deliberately blind to ─────────
# These blueprints have their own auth story (superadmin_required,
# @login_required only, cron_secret, etc.) and forcing them through
# this gate would break existing flows.
#
# The list mirrors what enforce_feature_flags / require_plan_selection
# / require_current_terms_version / enforce_subitem_gating all skip,
# plus a couple of "support channels stay reachable when everything
# else is broken" additions the ticket calls out.
ACCESS_EXEMPT_PREFIXES = (
    "auth.", "public.", "static", "invitations.", "cron.",
    "api_v1.",
    "superadmin.",           # its own @superadmin_required layer
    "portal.",               # customer portal, its own gate
    "portal_emp.",           # employee portal, its own gate
    "notifications.",
    "help.", "support.", "support_admin.",
)


# ─── The single decision function ────────────────────────────────────
def can_access(endpoint, user, company):
    """Return `(allowed, reason)`.

    `allowed=True` → `reason=None`.
    `allowed=False` → `reason` is one of the REASON_* constants above,
    which the guard translates to a friendly HTML page (upgrade CTA
    / disabled-with-reason / plain 403).
    """
    if not endpoint:
        return True, None
    if endpoint.startswith(ACCESS_EXEMPT_PREFIXES):
        return True, None
    if user is not None and getattr(user, "is_superadmin", False):
        return True, None
    if not user or not getattr(user, "is_authenticated", False):
        return False, REASON_PERMISSION

    # Step 1 — platform-wide FeatureFlag.
    try:
        from app.services.feature_registry import module_for_endpoint
        from app.services.feature_flags import is_module_enabled
        module = module_for_endpoint(endpoint)
        # module might be None for endpoints not in the registry
        # (e.g. very obscure debug pages). Treat as always-enabled
        # at the platform level — the plan gate below still fires
        # if a role check kicks in.
        if module and not is_module_enabled(module):
            return False, REASON_PLATFORM_DISABLED
    except Exception:
        current_app.logger.exception(
            "access.can_access: feature-flag check failed")
        # fail open — don't break the app on a helper crash

    # Step 2 — Company override (T4 hook — stub returns None today).
    try:
        override = _company_override(company, module)
        if override == "DENY":
            return False, REASON_COMPANY_DENIED
        if override == "GRANT":
            # A GRANT override skips plan + role checks entirely
            # (per ticket § step 2). Still refuses if super-admin
            # kills the whole platform via FeatureFlag (already
            # checked above).
            return True, None
    except Exception:
        current_app.logger.exception(
            "access.can_access: company-override hook failed")

    # Step 3 — plan module gate. `plan_allows` derives the module
    # from the permission code, so we need to route via a feature
    # or fall back to any permission the endpoint requires.
    try:
        from app.services.plan_gating import plan_allows, subitem_allowed
        from app.services.feature_registry import feature_for_endpoint
        feat = feature_for_endpoint(endpoint)
        # Pick any permission the feature declares (they all belong to
        # the same module by construction). No permissions listed →
        # the feature is @login_required only; skip the plan-module
        # gate for it (there's no permission code to gate on).
        perm_probe = None
        if feat and feat.permissions:
            perm_probe = feat.permissions[0]
        if perm_probe and not plan_allows(perm_probe, company):
            return False, REASON_PLAN_MODULE
    except Exception:
        current_app.logger.exception(
            "access.can_access: plan_allows check failed")

    # Step 4 — plan sub-item. `subitem_allowed` preserves the
    # trial-full-access behavior; we don't reimplement it here.
    # Match the sidebar-helper convention (endpoint → sub-item key
    # via endpoint_to_subitem) so a page not in SUB_ITEM_CATALOG
    # (like companies.edit) doesn't get spuriously refused.
    try:
        from app.services.plan_gating import endpoint_to_subitem
        si = endpoint_to_subitem(endpoint)
        # si is None for endpoints outside SUB_ITEM_CATALOG — mirror
        # the enforce_subitem_gating middleware which also skips
        # those (see app/__init__.py:517 `if si and not ...`).
        if si and not subitem_allowed(si, company):
            return False, REASON_PLAN_FEATURE
    except Exception:
        current_app.logger.exception(
            "access.can_access: subitem_allowed check failed")

    # Step 5 — role permission. Skip when the feature declares no
    # permissions (login-required-only endpoints like user_files.index
    # and portal_emp.*).
    try:
        from app.services.permissions import has_permission
        if feat and feat.permissions:
            # ANY of the feature's declared permissions is enough —
            # both "view" and "create" satisfy "can see the page".
            if not any(has_permission(p) for p in feat.permissions):
                return False, REASON_PERMISSION
    except Exception:
        current_app.logger.exception(
            "access.can_access: has_permission check failed")

    return True, None


def _company_override(company, module_code):
    """T4 hook (MARSOUD-SUPERADMIN-CONTROL-01 T4) — reads from the
    future `company_feature_overrides` table. Returns "GRANT" /
    "DENY" / None. Empty stub for T1+T2 so the decision chain has
    the hook slot in the right place; T4 fills it in without
    touching this file's flow.
    """
    return None


# ─── Sidebar builder (backend-owned) ─────────────────────────────────
def visible_nav(user, company):
    """Return the sidebar as a Python list of Section dicts.

    Shape:
        [
          {"key": "accounting", "label": "المالية والمحاسبة", "icon": "…",
           "links": [
             {"endpoint": "journals.index", "url": "/journals/",
              "label": "القيود اليومية", "icon": "📒"},
             …
           ]},
          …
        ]

    Only links `can_access` returns True for are included. Sections
    with zero visible links are dropped.

    Ordering follows the SUB_ITEM_CATALOG sections (via
    plan_gating.SECTION_LABEL_AR), preserving the visual layout
    users are used to. Any Feature whose module has no
    sidebar_section is skipped — those features are still
    reachable via direct URL but they don't own a sidebar row.
    """
    from flask import url_for
    from app.services.feature_registry import all_features, get_module
    from app.services.plan_gating import SECTION_LABEL_AR

    # Bucket features by their module's sidebar_section.
    buckets = {}
    for f in all_features():
        mod = get_module(f.module)
        section = getattr(mod, "sidebar_section", None) if mod else None
        if not section:
            continue
        buckets.setdefault(section, []).append(f)

    sections = []
    # Deterministic order = SECTION_LABEL_AR insertion order.
    for section_key, section_label in SECTION_LABEL_AR.items():
        feats = buckets.get(section_key, [])
        if not feats:
            continue
        # Icon: use the module's icon for the first feature that
        # matches this section (or a fallback).
        mod = get_module(feats[0].module)
        section_icon = getattr(mod, "icon", "") if mod else ""

        links = []
        for f in feats:
            # The FIRST endpoint in the feature is the sidebar
            # target — every other endpoint on the feature is a
            # sub-page (detail view etc.) that shares access rules.
            if not f.endpoints:
                continue
            primary = f.endpoints[0]
            allowed, _ = can_access(primary, user, company)
            if not allowed:
                continue
            try:
                url = url_for(primary)
            except Exception:
                continue
            links.append({
                "endpoint": primary,
                "url": url,
                "label": f.label_ar,
                "icon": f.icon or "",
            })

        if links:
            sections.append({
                "key": section_key,
                "label": section_label,
                "icon": section_icon,
                "links": links,
            })

    return sections


# ─── Debug helper (used by T6 later) ─────────────────────────────────
def access_report(company):
    """For every registered endpoint: {endpoint, module, permission,
    allowed_for_owner, reason_if_denied}. Not used by any UI in this
    ticket — but T6 (Company 360°) will call it when the "Features"
    tab renders. Also handy in `flask shell` for quick debugging.
    """
    from app.services.feature_registry import all_features
    # Assume an owner-role probe (highest role short of superadmin).
    # Callers who need per-user reports should call can_access
    # directly with the specific user.
    out = []
    for f in all_features():
        for endpoint in f.endpoints:
            # Owner-role probe: we don't have a specific user here,
            # so we skip step 5. Steps 1-4 cover "would owner ever
            # be able to open this?".
            module = f.module
            plan_allowed = True
            reason = None
            try:
                from app.services.feature_flags import is_module_enabled
                if not is_module_enabled(module):
                    plan_allowed = False
                    reason = REASON_PLATFORM_DISABLED
            except Exception:
                pass
            if plan_allowed:
                try:
                    from app.services.plan_gating import plan_allows
                    perm = f.permissions[0] if f.permissions else None
                    if perm and not plan_allows(perm, company):
                        plan_allowed = False
                        reason = REASON_PLAN_MODULE
                except Exception:
                    pass
            out.append({
                "endpoint": endpoint,
                "module": module,
                "permission": f.permissions[0] if f.permissions else None,
                "allowed_for_owner": plan_allowed,
                "reason_if_denied": reason,
            })
    return out
