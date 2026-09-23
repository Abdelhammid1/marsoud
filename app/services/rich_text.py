"""MARSOUD-RICH-TEXT-EDITOR-01 (2026-09-17) — Phase 1.

Server-side HTML sanitizer + a display-side renderer that safely
handles both the new HTML-shaped content the Quill editor emits AND
every plain-text description that already lives in the DB.

Two entry points:

  · sanitize_html(html)  — call on the POST path BEFORE storing.
                            Strips <script>, javascript:/data:
                            URLs, on* event handlers, any tag
                            we don't allow. Idempotent.

  · render_rich_text(v)  — call from the display side (Jinja
                            filter `rich_text`).  When the
                            stored value has ANY HTML tag, we
                            sanitize + return as safe Markup;
                            otherwise we treat it as legacy plain
                            text and do a proper escape + nl2br
                            so old descriptions still read
                            correctly.

Allowlist choices are conservative: everything on the ticket's
"Text Formatting" + "Text Structure" list (bold / italic / lists /
headings / links / code / blockquote / align) is in, everything
else is stripped.  If a follow-up phase needs images inline (per
"Attachments داخل التعليقات"), extend `_ALLOWED_TAGS` there — not
here — so the surface for Task Description stays deliberately
small.
"""
import re

import bleach
from bleach.css_sanitizer import CSSSanitizer
from markupsafe import Markup, escape


# ─── Allowlists ──────────────────────────────────────────────────────
_ALLOWED_TAGS = frozenset({
    # Block
    "p", "div", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre",
    # Inline formatting
    "strong", "b", "em", "i", "u", "s", "strike", "del", "mark",
    "sub", "sup", "small",
    "span", "code",
    # Anchors
    "a",
    # MARSOUD-RICH-TEXT-IMAGE-UPLOAD-01 (2026-09-23) — inline images
    # pasted through the Quill editor.  The `src` allowlist below
    # restricts them to our own upload path, so a hostile paste
    # can't reference an off-site tracking pixel or a base64 payload.
    "img",
})

# Per-tag attribute allowlist. `class` is allowed on a small set so
# Quill's `.ql-align-*` / `.ql-indent-*` classes survive.  Colour and
# highlight land in an inline `style="color:…"` — we let bleach
# through them via the CSSSanitizer below.
def _img_attrs(tag, name, value):
    """MARSOUD-RICH-TEXT-IMAGE-UPLOAD-01 (2026-09-23) — bleach
    attribute filter for `<img>`.  Called once per attribute on
    every img element.

    Return True → keep.  Return False → drop the attribute (the
    element itself stays, minus that attribute).  An `<img>` that
    loses its `src` renders as a broken image icon, which is the
    right behaviour for a rejected src (the reader can see that
    something was there and complain).

    Rules:
      · `src`:  MUST be a same-origin relative path starting with
                `/static/uploads/rich-text/`.  Absolute URLs,
                `data:` URIs, and any other path are dropped.
      · `alt`:  free-text, kept as-is (bleach handles quoting).
      · `title`: same.
      · anything else: dropped.
    """
    if name == "src":
        v = (value or "").strip()
        return v.startswith("/static/uploads/rich-text/")
    if name in ("alt", "title"):
        return True
    return False


_ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "span": ["class", "style"],
    "p": ["class"],
    "div": ["class"],
    "ol": ["start", "class"],
    "ul": ["class"],
    "li": ["class"],
    "code": ["class"],
    "pre": ["class"],
    "h1": ["class"], "h2": ["class"], "h3": ["class"],
    "h4": ["class"], "h5": ["class"], "h6": ["class"],
    "blockquote": ["class"],
    "img": _img_attrs,
}

# Which URL protocols links are allowed to point at.  `javascript:`
# and `data:` are absent — that's the whole point of the sanitizer.
_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto", "tel"})

# Which inline CSS properties can survive on a `style` attribute.
# Quill uses inline `color`, `background-color`, `text-align`,
# `direction` (RTL/LTR).  Everything else — position, display,
# transform, url(), — is stripped by CSSSanitizer.
_ALLOWED_CSS = [
    "color", "background-color", "text-align", "direction",
]


_css_sanitizer = CSSSanitizer(allowed_css_properties=_ALLOWED_CSS)


# Tags whose CONTENTS must be dropped too — not just the tag.  bleach's
# `strip=True` only drops the tag; it leaves the child text visible,
# so `<script>alert(1)</script>` would end up as "alert(1)" in the
# rendered page.  For these tags we pre-process and drop the whole
# element (opening tag, content, closing tag).
_STRIP_WITH_CONTENT = re.compile(
    r"<(script|style|iframe|object|embed|noscript)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Self-closing / unclosed versions of the same tags — someone could
# post `<script src=…>` with no closing tag; regex above won't catch
# it.  This one nukes any lone opening tag.
_STRIP_LONE_OPENERS = re.compile(
    r"<(script|style|iframe|object|embed|noscript)\b[^>]*/?>",
    re.IGNORECASE,
)


def sanitize_html(html):
    """Return a safe HTML string.  `None` / empty / whitespace-only
    → returns "" so callers can persist without a null check.

    Callers MUST run this before writing to the DB.  Rendering paths
    trust the DB content is already clean, so a raw-user-input write
    somewhere else (a background job, a data migration) would poison
    every future view of that row."""
    if not html or not str(html).strip():
        return ""
    # Pre-pass: drop dangerous elements (script/style/iframe/…)
    # AND their contents.  bleach.clean(strip=True) only removes
    # the tag; without this pre-pass, `<script>alert(1)</script>`
    # would leak "alert(1)" as visible plain text.
    working = str(html)
    working = _STRIP_WITH_CONTENT.sub("", working)
    working = _STRIP_LONE_OPENERS.sub("", working)
    cleaned = bleach.clean(
        working,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        css_sanitizer=_css_sanitizer,
        strip=True,             # drop disallowed tags rather than
                                #   escape them into visible <script>
        strip_comments=True,
    )
    # Every link opens in a new tab with the noopener guard so a
    # tenant's malicious page can't reach back into our origin via
    # window.opener.  bleach.linkify would ADD this to plain URLs
    # but the editor emits <a> tags already; a linkify pass would
    # double-wrap them.
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[_open_in_new_tab],
        skip_tags=["pre", "code"],
    )
    return cleaned


def _open_in_new_tab(attrs, new=False):
    """bleach.linkify callback — forces `target=_blank` +
    `rel=noopener noreferrer` on every link.  `attrs` is a dict
    keyed by (namespace, name) tuples."""
    key_target = (None, "target")
    key_rel = (None, "rel")
    attrs[key_target] = "_blank"
    attrs[key_rel] = "noopener noreferrer"
    return attrs


# ─── Display side ───────────────────────────────────────────────────
def render_rich_text(value):
    """Jinja filter (`{{ description|rich_text }}`).

    Two entry cases:

      · The value's first non-whitespace character is `<` → treat as
        Quill-emitted HTML.  Re-sanitize (defense in depth) + emit
        as Markup so Jinja renders unescaped.
      · Anything else → legacy plain text.  Escape it (so a stray
        `<` in "a<b, y>x" doesn't disappear) + convert `\\n` to
        `<br>` so paragraph breaks on legacy content still show.

    The "first non-whitespace" heuristic is deliberate:  Quill
    always wraps its output in a block tag (`<p>`, `<h1>`, `<ul>`,
    …), so its output always starts with `<`.  Plain text never
    does.  A middle-of-string `<b, y>` in legacy content is
    unambiguous with this rule — it stays legacy, gets escaped.
    A None / empty value → Markup("") so `{% if x %}` guards still
    work; falsy stays falsy.
    """
    if not value:
        return Markup("")
    text = str(value)
    if text.lstrip().startswith("<"):
        return Markup(sanitize_html(text))
    # Legacy plain text.  Escape then convert newlines to <br>.
    escaped = str(escape(text))
    return Markup(escaped.replace("\n", "<br>"))
