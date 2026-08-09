"""MARSOUD-SUPERADMIN-CONTROL-01 T8 (2026-08-08) — route coverage
audit.

The ticket's mandate: 'مستحيل تتسلّم شاشة من غير طريقة توصل ليها'.
This module walks the URL map, classifies each endpoint (HTML
page / API / POST-only / exempt), scans every template AND every
route file for `url_for(...)` mentions, and reports the pages
that no template links to.

Called from two places:
  · `flask audit-routes` CLI (registered in app/__init__.py) —
    the enforcement surface; exits nonzero on any orphan not in
    `route_audit_ignore.txt`.
  · `GET /admin/routes` — the read-only super-admin viewer that
    renders the same data with filters.

Both call `build_coverage(app)` — cheap-ish (reads 259 templates
+ 52 route files), no cache. Called once per invocation.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ─── Config ────────────────────────────────────────────────────────
# Prefixes we never audit: they're JSON APIs, background workers,
# auth flow, or Flask internals. Nothing in this list gets a
# template link because nothing SHOULD link to them.
EXEMPT_PREFIXES = (
    "static", "auth.", "public.", "cron.", "api_v1.",
    "invitations.",
)

# Endpoints we skip when scanning "templates for url_for" — the
# scanner picks these up as false-positive references because they
# appear in unrelated regex matches.
# (Nothing here today, but leave the hook.)
_SCAN_SKIP_ENDPOINTS = frozenset()


# ─── Data class ────────────────────────────────────────────────────
@dataclass(frozen=True)
class EndpointCoverage:
    endpoint: str
    url_rule: str
    methods: frozenset
    category: str            # "page" / "api" / "post_only" / "exempt"
                             # / "parametric_page"
    module: Optional[str]    # from feature_registry, if known
    linked_from: tuple       # tuple[str, ...] of paths (repo-relative)
    ignored_reason: Optional[str]
    is_orphan: bool


# ─── Ignore file ────────────────────────────────────────────────────
_IGNORE_FILE_NAME = "route_audit_ignore.txt"


def _repo_root() -> Path:
    """Repo root is where the ignore file + app/ + tests/ live."""
    # app/services/route_audit.py → up two = repo root.
    return Path(__file__).resolve().parent.parent.parent


def _read_ignore_file() -> dict:
    """Parse `route_audit_ignore.txt` at repo root.

    Format per non-comment line:
        endpoint.name  # required reason text

    Empty lines and lines starting with `#` are skipped. Any
    non-blank line WITHOUT a `#` reason raises ValueError — the
    ticket says every entry needs a reason.
    """
    path = _repo_root() / _IGNORE_FILE_NAME
    if not path.exists():
        return {}
    out = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if "#" not in line:
            raise ValueError(
                f"{_IGNORE_FILE_NAME}:{lineno}: ignore entry has no "
                f"reason. Format: `endpoint.name  # reason text`.\n"
                f"  Offending line: {line!r}")
        ep_part, _, reason_part = line.partition("#")
        ep = ep_part.strip()
        reason = reason_part.strip()
        if not ep:
            continue
        if not reason:
            raise ValueError(
                f"{_IGNORE_FILE_NAME}:{lineno}: reason is empty. "
                f"Format: `endpoint.name  # reason text`.")
        out[ep] = reason
    return out


# ─── Classification ────────────────────────────────────────────────
def _classify_rule(rule) -> str:
    ep = rule.endpoint
    if any(ep.startswith(p) for p in EXEMPT_PREFIXES):
        return "exempt"
    if rule.rule.startswith("/api/"):
        return "api"
    if "GET" not in rule.methods:
        return "post_only"
    # Has `<converter:name>` parts → parametric. Still a page; the
    # link-detection accepts `url_for('endpoint')` regardless of
    # whether the caller passes args.
    if "<" in rule.rule:
        return "parametric_page"
    return "page"


# ─── Template + Python scanners ────────────────────────────────────
# `url_for('endpoint')` OR `url_for("endpoint")` in a template or a
# Python file. Also matches url_for called with args after — we
# only grab the endpoint name string, which is the FIRST literal
# arg.
_URL_FOR_RE = re.compile(
    r"""url_for\(\s*['"]([a-zA-Z_][a-zA-Z0-9_.]*)['"]"""
)


def _scan_dir_for_url_for(root: Path, glob_pattern: str) -> dict:
    """Walk `root` for files matching `glob_pattern`, regex out every
    url_for('endpoint') mention. Returns endpoint → list of
    file paths that reference it (repo-relative)."""
    out = {}
    if not root.exists():
        return out
    for f in root.rglob(glob_pattern):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        matches = set(_URL_FOR_RE.findall(text))
        if not matches:
            continue
        rel = str(f.relative_to(_repo_root()))
        for ep in matches:
            out.setdefault(ep, []).append(rel)
    return out


def _scan_sidebar_tuple_lists() -> dict:
    """Extra pass over the two known sidebar-tuple-list files. Some
    role-specific link lists in `app/templates/base.html` and
    `app/templates/admin/base.html` use tuple literals like:

        ('portal_emp.custody_list', 'عهدتي', '💵')

    …and DON'T call url_for on the string directly (the loop
    later calls url_for on the tuple's first element). The generic
    regex above would miss these. This scanner catches them so a
    role-only sidebar row doesn't show as orphaned."""
    out = {}
    root = _repo_root() / "app" / "templates"
    for rel_path in ("base.html", "admin/base.html"):
        f = root / rel_path
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        # Match tuple-first-string patterns like ('endpoint.name', ...
        for m in re.finditer(
            r"""\(\s*['"]([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z0-9_]+)['"]""",
            text
        ):
            ep = m.group(1)
            out.setdefault(ep, []).append(f"app/templates/{rel_path}")
    return out


def _linked_endpoints() -> dict:
    """Combine template scan + Python redirect scan + sidebar
    tuple scan. Returns endpoint → list[file_path]."""
    root = _repo_root()
    combined = {}
    # Templates
    tpl_hits = _scan_dir_for_url_for(root / "app" / "templates", "*.html")
    for ep, paths in tpl_hits.items():
        combined.setdefault(ep, []).extend(paths)
    # Python route files (POST → redirect(url_for(...)) is a valid
    # entry point too).
    py_hits = _scan_dir_for_url_for(root / "app" / "routes", "*.py")
    for ep, paths in py_hits.items():
        combined.setdefault(ep, []).extend(paths)
    # Also scan app/__init__.py (before_request redirects like
    # require_plan_selection → auth.choose_plan)
    init = root / "app" / "__init__.py"
    if init.exists():
        try:
            for m in _URL_FOR_RE.findall(
                init.read_text(encoding="utf-8")
            ):
                combined.setdefault(m, []).append("app/__init__.py")
        except (UnicodeDecodeError, PermissionError):
            pass
    # Role-specific sidebar tuple lists (extra pass).
    for ep, paths in _scan_sidebar_tuple_lists().items():
        combined.setdefault(ep, []).extend(paths)
    # Dedup preserving order.
    for ep, paths in combined.items():
        combined[ep] = sorted(set(paths))
    return combined


# ─── Module classification (best-effort, T1 registry if available) ─
def _module_for(endpoint: str) -> Optional[str]:
    """Prefer the feature-registry lookup (T1) if it's importable;
    fall back to the blueprint prefix otherwise. Keeps T8 usable
    on branches where T1+T2 haven't merged yet."""
    try:
        from app.services.feature_registry import module_for_endpoint
        m = module_for_endpoint(endpoint)
        if m:
            return m
    except Exception:
        pass
    # Fallback: bare blueprint prefix
    return endpoint.split(".", 1)[0] if "." in endpoint else None


# ─── The engine ────────────────────────────────────────────────────
def build_coverage(app) -> list:
    """Walk every rule in app.url_map, classify, cross-check
    against the template/Python link scan, and against the ignore
    file. Returns a list of EndpointCoverage — one row per rule.

    NOTE: iter_rules can yield DUPLICATES of the same endpoint
    (Flask registers HEAD/OPTIONS as separate rules for the same
    view). We dedup on endpoint here so the report shows each
    endpoint once."""
    links_by_ep = _linked_endpoints()
    ignore = _read_ignore_file()
    seen_endpoints = set()
    out = []
    for rule in app.url_map.iter_rules():
        ep = rule.endpoint
        if ep in seen_endpoints:
            continue
        seen_endpoints.add(ep)
        category = _classify_rule(rule)
        # Filter Flask's automatic HEAD/OPTIONS from the displayed
        # methods so users see the semantic set.
        methods = frozenset(rule.methods or set()) - {"HEAD", "OPTIONS"}
        linked = links_by_ep.get(ep, [])
        # Fail loud on stale ignore entries — an endpoint listed in
        # the ignore file that no longer exists is bit-rot.
        ignored_reason = ignore.get(ep)
        # `is_orphan` only applies to real pages
        is_orphan = (
            category in ("page", "parametric_page")
            and not linked
            and ignored_reason is None
            and ep not in _SCAN_SKIP_ENDPOINTS
        )
        out.append(EndpointCoverage(
            endpoint=ep,
            url_rule=rule.rule,
            methods=methods,
            category=category,
            module=_module_for(ep),
            linked_from=tuple(linked),
            ignored_reason=ignored_reason,
            is_orphan=is_orphan,
        ))
    out.sort(key=lambda r: r.endpoint)
    return out


def orphans(app) -> list:
    """The subset of build_coverage() where is_orphan=True."""
    return [r for r in build_coverage(app) if r.is_orphan]


def stale_ignores(app) -> list:
    """Ignore entries that name endpoints not in the url_map.
    Called by the CLI so a rename bit-rots loudly, not silently."""
    ignore = _read_ignore_file()
    live_eps = {r.endpoint for r in app.url_map.iter_rules()}
    return sorted(ep for ep in ignore if ep not in live_eps)


def summary(app) -> dict:
    """Convenience aggregates for the admin page's summary cards."""
    rows = build_coverage(app)
    return {
        "total": len(rows),
        "pages": sum(1 for r in rows
                     if r.category in ("page", "parametric_page")),
        "orphans": sum(1 for r in rows if r.is_orphan),
        "api": sum(1 for r in rows if r.category == "api"),
        "post_only": sum(1 for r in rows if r.category == "post_only"),
        "exempt": sum(1 for r in rows if r.category == "exempt"),
        "ignored": sum(1 for r in rows if r.ignored_reason is not None),
    }
