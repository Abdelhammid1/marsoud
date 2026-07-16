"""MARSOUD-LINKIFY (Abdelhamid 2026-07-16) — turn URLs inside
free-text into clickable anchors.

Composable with `render_mentions` from services.mentions — mentions
run first (they produce trusted anchor tags with our own href
patterns), then this pass rewrites remaining plain-text URLs.
Since mentions have already produced Markup, we recognise them by
their `<a href="/tasks/?scope=employees…"` pattern and skip them
during the pass.

Design constraints:
  · Never emit unescaped user input — everything that isn't a
    recognised URL is passed through markupsafe.escape.
  · Recognise http:// / https:// / www. prefixes only. Bare
    "example.com" without www is deliberately NOT autolinked to
    avoid false positives on Arabic text with mixed punctuation.
  · target="_blank" + rel="noopener noreferrer" for external
    navigation safety.
"""
import re

from markupsafe import escape, Markup


# Matches URLs starting with http://, https://, or www.
# Stops at whitespace or common trailing punctuation. Kept
# purposefully simple — we skip ASCII-only heuristics that Arabic
# comment text would trip over.
_URL_RE = re.compile(
    r"(?P<url>(?:https?://|www\.)[^\s<>\"'()]+)",
    re.UNICODE,
)


def _render_anchor(url):
    href = url if url.startswith(("http://", "https://")) else f"https://{url}"
    # We escape both the href and the visible label — belt & braces,
    # even though the regex already excludes < > " '.
    return (
        f'<a href="{escape(href)}" target="_blank" '
        f'rel="noopener noreferrer" '
        f'class="text-brand-600 hover:underline break-all">'
        f'{escape(url)}</a>'
    )


def render_linkify(text):
    """Escape `text`, then rewrite any URL it contains as an anchor.

    Accepts str or existing Markup. When given Markup, we honour any
    HTML that's already in it — we only linkify the plain-text
    segments so the mentions filter's <a> tags survive intact.
    """
    if not text:
        return Markup("")

    # If the input is already Markup (e.g. after `| mentions`), we
    # walk it as HTML fragments: text OUTSIDE anchor tags gets
    # linkified; text INSIDE existing anchors is left alone. Anchor
    # boundaries are matched with a permissive regex that captures
    # the whole <a ...>...</a> block.
    if isinstance(text, Markup):
        html = str(text)
        pieces = []
        anchor_re = re.compile(
            r"(<a\b[^>]*>.*?</a>)", re.IGNORECASE | re.DOTALL,
        )
        pos = 0
        for m in anchor_re.finditer(html):
            # Linkify the text BEFORE this anchor.
            pre = html[pos:m.start()]
            pieces.append(_linkify_plain_html(pre))
            # Keep the anchor block verbatim — it's already safe HTML.
            pieces.append(m.group(1))
            pos = m.end()
        pieces.append(_linkify_plain_html(html[pos:]))
        return Markup("".join(pieces))

    # Plain str — escape everything and linkify URLs.
    return Markup(_linkify_plain_str(str(text)))


def _linkify_plain_str(text):
    """Escape + linkify a raw string. Returns unicode with HTML
    ready to be wrapped in Markup by the caller."""
    parts = []
    last = 0
    for m in _URL_RE.finditer(text):
        # Escape the plain-text segment BEFORE the URL.
        parts.append(str(escape(text[last:m.start()])))
        parts.append(_render_anchor(m.group("url")))
        last = m.end()
    parts.append(str(escape(text[last:])))
    return "".join(parts)


def _linkify_plain_html(html):
    """Linkify a segment of HTML that has NO anchor tags. Callers
    already carved out the anchors — everything else is treated as
    escaped-once plaintext to be rescanned. Preserves any HTML tags
    that came through mentions (badge spans) by re-escaping only
    the visible text portions."""
    # This segment came from an escaped source (mentions did its
    # own escape), so we DON'T re-escape — we only rewrite URLs
    # that survived the escape (they'll be like http:&#x2F;&#x2F;...).
    # Simpler + safer: apply the URL rewrite on the raw HTML text.
    # Because our URL regex forbids < > " ', an escaped entity
    # sequence won't match — we operate only on the natural URL
    # substrings.
    parts = []
    last = 0
    for m in _URL_RE.finditer(html):
        parts.append(html[last:m.start()])
        parts.append(_render_anchor(m.group("url")))
        last = m.end()
    parts.append(html[last:])
    return "".join(parts)
