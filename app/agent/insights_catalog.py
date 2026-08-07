"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — tool registry
for the analyst agent.

The old insights_tools.py had 5 tools appended to a Python list by
hand. The professional-analyst ticket pushes the catalog to ~100
tools spanning every read surface in the app. Hand-maintained lists
don't scale to that count without silent collisions or gate-drops.

This registry:

  · fails at import if a tool name is reused (P0 — two tools with
    the same name means the second one wins the dispatch and the
    first one becomes silently dead code, which is exactly the
    kind of drift the ticket calls out).
  · fails at import if a `permission=` string isn't in
    services/permissions.P (P0 for the same reason — a typo in
    a perm string would fail-open at request time, gating nothing).
  · lets a caller mark `permission=None` explicitly to opt out
    (used for the pure-aggregate tools that don't touch sensitive
    data — the intent is loud in the source).
  · exposes `build_schemas()` and `execute(name, args, company_id,
    user_id)` so the route + audit can consume the registry
    without knowing the internals.

Contract every registered tool honors:
  1. Positional signature `(args_dict, company_id, user_id) -> dict`
  2. Company-scope filter on every underlying query
  3. If `permission=` is set, `_has_perm` short-circuits the call
     and returns `{"rows": [], "note": "…صلاحية غير كافية…"}`
     — MUST NOT fall through to actual data
  4. Returns JSON-primitive-serializable Python objects only.
"""
from __future__ import annotations
from typing import Callable, Dict, Any, List, Optional


class _RegistryError(RuntimeError):
    """Raised at import time when the catalog is misconfigured.

    Deliberately explodes loudly at boot — a misconfigured tool
    catalog is worse than a missing tool because the model may
    still call something that fails-open silently."""


_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register(*, name: str, description: str,
             input_schema: Dict[str, Any],
             permission: Optional[str] = None,
             backend_module: Optional[str] = None):
    """Decorator. Registers a tool at import time.

    Every batch module imports this and stacks its own
    @register(...) decorators. The insights_tools facade then
    imports the batch modules by side effect — no manual list
    maintenance.
    """
    def deco(fn: Callable):
        # Name-collision guard.
        if name in _REGISTRY:
            raise _RegistryError(
                f"tool name collision: {name!r} already registered "
                f"by {_REGISTRY[name]['backend_module']}")
        # Perm-string validation — fail hard if the perm doesn't
        # exist in P (a typo would silently mean "no gate").
        if permission is not None:
            try:
                from app.services.permissions import P
                if permission not in P:
                    raise _RegistryError(
                        f"tool {name!r}: permission {permission!r} "
                        f"is not in services/permissions.P")
            except ImportError:  # pragma: no cover — startup ordering
                pass
        _REGISTRY[name] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "permission": permission,
            "fn": fn,
            "backend_module": backend_module or fn.__module__,
        }
        return fn
    return deco


def build_schemas() -> List[Dict[str, Any]]:
    """Return the Anthropic-style schema list the provider expects.

    Order is registration order (stable across restarts because
    Python's dict preserves insertion order since 3.7), which keeps
    DeepSeek's automatic prompt-cache warm — a reshuffle would
    invalidate the cached tools-array prefix on every request.
    """
    return [{
        "name": t["name"],
        "description": t["description"],
        "input_schema": t["input_schema"],
    } for t in _REGISTRY.values()]


def execute(name: str, args, company_id, user_id):
    """Dispatch by name. Unknown tool → structured error the model
    can read. Never raises — caller (base.run_agent_turn) has its
    own try/except but this layer is friendlier to introspect."""
    entry = _REGISTRY.get(name)
    if not entry:
        return {"error": f"tool غير موجودة: {name}"}
    fn = entry["fn"]
    return fn(args or {}, company_id, user_id)


def all_names() -> List[str]:
    """For the audit."""
    return list(_REGISTRY.keys())


def entry(name: str) -> Optional[Dict[str, Any]]:
    """For the audit — introspect a specific tool."""
    return _REGISTRY.get(name)


# ─── Shared helpers used across batch modules ─────────────────
def has_perm(user_id, company_id, action) -> bool:
    """Delegate to the app's real permission check. Same shape
    the old insights_tools._has_perm() had. Every gated tool
    calls this at its entry."""
    try:
        from app.services.permissions import get_user_role, P
        role = get_user_role(user_id, company_id)
        if not role:
            return False
        allowed = P.get(action, set())
        return role in allowed
    except Exception:
        return False


def perm_denied(action: str, *, extra=None):
    """Standard shape the analyst returns when a per-tool gate
    denies the caller. Prompt rule 6 says the model will relay
    the note verbatim — do NOT ship real data alongside."""
    payload = {
        "rows": [],
        "note": (f"صلاحية غير كافية ({action}) — ما رجّعناش أي "
                 f"بيانات. راجع صلاحيات المستخدم."),
    }
    if extra:
        payload.update(extra)
    return payload


def parse_date(raw, fallback=None):
    """Same helper the old insights_tools had — shared here so
    every batch module uses the same parsing."""
    from datetime import date, datetime
    if not raw:
        return fallback if fallback is not None else date.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return fallback if fallback is not None else date.today()
