"""MARSOUD-P111 — Auto-generated live system map from actual code & templates.

Instead of relying on manually written help articles that go stale,
this module inspects the live Flask app (`feature_registry`, `industry_presets`,
`url_map`, and Jinja templates) to build an accurate, up-to-date catalog of
every screen, module, workflow, and UI form field in Marsoud.
"""
from __future__ import annotations
import os
import re
from flask import current_app, url_for

_CACHE = None


def _strip_tags(html_text: str) -> str:
    txt = re.sub(r"{%.*?%}", " ", html_text, flags=re.S)
    txt = re.sub(r"{{.*?}}", " ", txt, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _extract_template_summary(rel_template: str) -> dict:
    """Safely extract Arabic headings, labels, form fields, and buttons from a template."""
    tpl_root = os.path.join(current_app.root_path, "templates")
    full_path = os.path.normpath(os.path.join(tpl_root, rel_template))
    if not full_path.startswith(tpl_root) or not os.path.isfile(full_path):
        return {}
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception:
        return {}

    headings = []
    for m in re.finditer(r"<h[1-3][^>]*>(.*?)</h[1-3]>", raw, flags=re.S | re.I):
        t = _strip_tags(m.group(1))
        if t and len(t) <= 80 and t not in headings:
            headings.append(t)

    labels = []
    for m in re.finditer(r"<label[^>]*>(.*?)</label>", raw, flags=re.S | re.I):
        t = _strip_tags(m.group(1)).rstrip("*:")
        if t and 2 <= len(t) <= 60 and t not in labels:
            labels.append(t)

    inputs = []
    for m in re.finditer(r"<(?:input|select|textarea)\b([^>]*)>", raw, flags=re.S | re.I):
        attrs = m.group(1)
        type_m = re.search(r'\btype=["\']([^"\']+)["\']', attrs, flags=re.I)
        itype = (type_m.group(1).lower() if type_m else "text")
        if itype in ("hidden", "submit", "button", "csrf"):
            continue
        name_m = re.search(r'\bname=["\']([^"\']+)["\']', attrs, flags=re.I)
        if not name_m:
            continue
        fname = name_m.group(1)
        if fname in ("csrf_token",):
            continue
        req = bool(re.search(r'\brequired\b', attrs, flags=re.I))
        ph_m = re.search(r'\bplaceholder=["\']([^"\']+)["\']', attrs, flags=re.I)
        ph = _strip_tags(ph_m.group(1)) if ph_m else ""
        entry = f"{fname}" + (" (إجباري)" if req else "") + (f" [{ph}]" if ph else "")
        if entry not in inputs:
            inputs.append(entry)

    buttons = []
    for m in re.finditer(r"<button[^>]*>(.*?)</button>", raw, flags=re.S | re.I):
        t = _strip_tags(m.group(1))
        if t and 2 <= len(t) <= 45 and t not in buttons:
            buttons.append(t)

    return {
        "template": rel_template,
        "headings": headings[:6],
        "labels": labels[:25],
        "fields": inputs[:25],
        "buttons": buttons[:12],
    }


def _map_endpoints_to_templates() -> dict[str, str]:
    """Scan route files to map Flask endpoint names to their primary render_template() file."""
    routes_dir = os.path.join(current_app.root_path, "routes")
    ep_to_tpl = {}
    if not os.path.isdir(routes_dir):
        return ep_to_tpl

    for fname in os.listdir(routes_dir):
        if not fname.endswith(".py") or fname.startswith("__"):
            continue
        fpath = os.path.join(routes_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except Exception:
            continue
        bp_m = re.search(r'Blueprint\(\s*["\']([^"\']+)["\']', src)
        if not bp_m:
            continue
        bp_name = bp_m.group(1)
        # Split by @bp.route
        parts = re.split(r"(?=@\w+\.route\()", src)
        for part in parts:
            if not part.startswith("@"):
                continue
            fn_m = re.search(r"def\s+([a-zA-Z0-9_]+)\s*\(", part)
            tpl_m = re.search(r'render_template\(\s*["\']([^"\']+\.html)["\']', part)
            if fn_m and tpl_m:
                ep = f"{bp_name}.{fn_m.group(1)}"
                ep_to_tpl[ep] = tpl_m.group(1)
    return ep_to_tpl


def get_system_map(force_refresh: bool = False) -> dict:
    """Build (and cache in memory) the live system map from code."""
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    from app.services.feature_registry import all_modules, all_features
    from app.services.industry_presets import INDUSTRY_PRESETS

    ep_to_tpl = _map_endpoints_to_templates()

    modules_list = []
    mod_by_code = {}
    for m in all_modules(include_core=True):
        item = {
            "code": m.code,
            "label_ar": m.label_ar,
            "label_en": m.label_en,
            "description_ar": m.description_ar,
            "category": m.category,
            "is_core": m.is_core,
        }
        modules_list.append(item)
        mod_by_code[m.code] = item

    screens = []
    seen_urls = set()
    for feat in all_features():
        primary_ep = feat.endpoints[0] if feat.endpoints else None
        url = ""
        if primary_ep:
            try:
                url = url_for(primary_ep)
            except Exception:
                url = ""
        tpl = ep_to_tpl.get(primary_ep, "") if primary_ep else ""
        mod_info = mod_by_code.get(feat.module, {})
        screens.append({
            "code": feat.code,
            "module": feat.module,
            "module_ar": mod_info.get("label_ar", feat.module),
            "label_ar": feat.label_ar,
            "url": url,
            "endpoint": primary_ep or "",
            "template": tpl,
        })
        if url:
            seen_urls.add(url)

    # Also harvest concrete GET routes from Flask url_map that render templates
    # (catches /new, /form, /settings, /hub pages not explicitly listed as sidebar features)
    for rule in current_app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        if rule.arguments:
            continue
        ep = rule.endpoint
        if ep.startswith(("static", "superadmin.", "api_v1", "auth.", "public.", "cron.")):
            continue
        url = str(rule.rule)
        if url in seen_urls:
            continue
        tpl = ep_to_tpl.get(ep, "")
        if not tpl:
            continue
        summary = _extract_template_summary(tpl)
        title = (summary.get("headings") or [ep])[0]
        mod_guess = ep.split(".", 1)[0]
        screens.append({
            "code": ep,
            "module": mod_guess,
            "module_ar": mod_by_code.get(mod_guess, {}).get("label_ar", mod_guess),
            "label_ar": title,
            "url": url,
            "endpoint": ep,
            "template": tpl,
        })
        seen_urls.add(url)

    presets = []
    for code, p in INDUSTRY_PRESETS.items():
        presets.append({
            "code": code,
            "label_ar": p.get("label_ar", code),
            "description_ar": p.get("description_ar", ""),
            "features": p.get("features", {}),
        })

    _CACHE = {
        "modules": modules_list,
        "screens": screens,
        "presets": presets,
        "ep_to_tpl": ep_to_tpl,
    }
    return _CACHE


def inspect_screen_details(query_or_url: str) -> dict:
    """Find a screen by URL, endpoint, or Arabic keyword and parse its actual HTML form/UI."""
    smap = get_system_map()
    q = (query_or_url or "").strip().lower()
    if not q:
        return {"error": "حدد اسم الشاشة أو رابطها"}

    matched = None
    # 1. Exact URL match
    for s in smap["screens"]:
        if s["url"] and s["url"].lower() == q:
            matched = s
            break
    # 2. Substring match on URL, label_ar, or code
    if not matched:
        for s in smap["screens"]:
            if q in (s["url"] or "").lower() or q in (s["label_ar"] or "").lower() or q in (s["code"] or "").lower():
                matched = s
                break

    if not matched:
        return {"found": False, "message": f"لم أجد شاشة مطابقة لـ: {query_or_url}"}

    ui_info = _extract_template_summary(matched.get("template", "")) if matched.get("template") else {}
    return {
        "found": True,
        "label_ar": matched["label_ar"],
        "module_ar": matched["module_ar"],
        "url": matched["url"],
        "headings": ui_info.get("headings", []),
        "form_labels": ui_info.get("labels", []),
        "form_fields": ui_info.get("fields", []),
        "buttons": ui_info.get("buttons", []),
    }
