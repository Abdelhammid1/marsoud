"""MARSOUD-MENTIONS — parse @-mentions in comment text + fan-out
notifications.

Design (Abdelhamid's spec 2026-07-11):
  · Store mentions inline as `@[Display Name](user:ID)` tokens so
    the user-id survives a name change (the display text can drift
    without breaking the reference).
  · One notification per mentioned user, deduped across the same
    comment — mentioning someone twice does NOT double-ping.
  · The actor themselves is silently excluded (no self-ping).
  · Each surface (Task / Lead / Project / …) supplies its own
    entity_label + link_url; this module knows nothing about the
    parent entity.

Public surface used by the callers:
  · parse_mention_ids(text) → set[int]
  · notify_mentions(*, actor_user_id, mentioned_user_ids,
                       company_id, entity_kind, entity_label,
                       link_url, snippet)
  · render_mentions(text) → HTML with tokens replaced by links
"""
import re
from typing import Set

from app import db
from app.models import Company, User, Notification, NotificationKind


# `@[Name with spaces](user:12345)`
# The Name group is non-greedy, allows anything except `]`, and the
# user-id group is digits only so a malformed token can't inject SQL
# or route confusion downstream.
MENTION_RE = re.compile(r"@\[([^\]]+)\]\(user:(\d+)\)")


def parse_mention_ids(text) -> Set[int]:
    """Extract every distinct user_id referenced by a mention token.

    Returns an empty set for None / empty / no-mention input so
    callers can iterate the result unconditionally. Deduped — the
    ticket calls this out explicitly ("مذكر مرتين → إشعار واحد")."""
    if not text:
        return set()
    ids = set()
    for match in MENTION_RE.finditer(text):
        try:
            ids.add(int(match.group(2)))
        except (TypeError, ValueError):
            continue
    return ids


def notify_mentions(*, actor_user_id, mentioned_user_ids, company_id,
                       entity_kind, entity_label, link_url, snippet):
    """Fire one Notification per mentioned user (+ trigger email).

    Guards:
      · Actor themselves is silently excluded (no self-mention noise).
      · Users who don't belong to `company_id` are dropped (a
        malicious edit shouldn't be able to notify a stranger).
      · Users who no longer exist are dropped.

    Best-effort emails: SMTP failures never block the in-app
    notification insert."""
    from app.models.user import user_companies
    ids = set(mentioned_user_ids or []) - {actor_user_id}
    if not ids:
        return 0

    # Filter to users who are members of the current company.
    valid_rows = db.session.execute(
        db.select(user_companies.c.user_id).where(
            user_companies.c.user_id.in_(ids),
            user_companies.c.company_id == company_id,
        )
    ).fetchall()
    valid_ids = {r[0] for r in valid_rows}
    if not valid_ids:
        return 0

    # Look up display data for actor + entities once.
    actor = db.session.get(User, actor_user_id) if actor_user_id else None
    actor_name = (actor.full_name if actor else "شخص ما") or "شخص ما"

    trimmed = (snippet or "").strip()
    if len(trimmed) > 200:
        trimmed = trimmed[:197] + "…"

    title = f"💬 {actor_name} ذكرك في {entity_label}"
    fan_out = 0
    for uid in valid_ids:
        try:
            n = Notification(
                company_id=company_id,
                user_id=uid,
                kind=NotificationKind.MENTION.value,
                title=title,
                body=trimmed,
                link_url=link_url,
            )
            db.session.add(n)
            db.session.flush()
        except Exception:
            db.session.rollback()
            continue

        fan_out += 1

        # Best-effort email — SMTP failure never blocks the bell.
        try:
            _send_mention_email(
                user_id=uid,
                actor_name=actor_name,
                entity_kind=entity_kind,
                entity_label=entity_label,
                link_url=link_url,
                snippet=trimmed,
                # MARSOUD-MENTION-EMAIL-FIX (2026-08-13) — pass
                # the company through so the shared email
                # shell (_base.html) can pick up the tenant's
                # logo + name for the header, matching every
                # other transactional email.
                company_id=company_id,
            )
        except Exception:
            import logging
            logging.getLogger("marsoud.mentions").exception(
                "mention email send failed for user %s", uid,
            )
    db.session.commit()
    return fan_out


def _send_mention_email(*, user_id, actor_name, entity_kind,
                             entity_label, link_url, snippet,
                             company_id=None):
    """Render + send the mention notification email.

    MARSOUD-MENTION-EMAIL-FIX (2026-08-13) —
      · Callers supply `link_url` as an ABSOLUTE URL (they
        build it with `_external=True`). The old
        `SERVER_URL` prefix hack read a config key that
        was never set, so the button rendered as an inert
        relative href in Gmail / Outlook.
      · `company_id` is resolved best-effort into a
        `Company` object and passed as `company=` to the
        template, so the shared shell picks up the
        tenant's logo + name. Any resolution failure
        downgrades to the default header — the email is
        still sent (fail-safe requirement from the DoD).
    """
    from app.services.email import send_email
    from flask import render_template
    u = db.session.get(User, user_id)
    if not u or not (u.email or "").strip():
        return
    co = None
    if company_id:
        try:
            co = db.session.get(Company, company_id)
        except Exception:
            co = None
    html = render_template(
        "emails/mention.html",
        recipient=u, actor_name=actor_name,
        entity_kind=entity_kind, entity_label=entity_label,
        link_url=link_url, snippet=snippet,
        company=co,
    )
    subject = f"💬 {actor_name} ذكرك في {entity_label}"
    send_email(u.email, subject, html)


def render_mentions(text) -> str:
    """Replace `@[Name](user:ID)` tokens with an anchor tag pointing
    at that user's tasks board. HTML-escapes both the surrounding
    plain text AND the display name to avoid XSS — single pass so
    we don't double-escape `<` → `&amp;lt;`.

    Registered as a Jinja filter named `mentions` so any comment
    template can render user-friendly names.
    """
    if not text:
        return ""
    from markupsafe import escape, Markup

    parts = []
    last = 0
    for match in MENTION_RE.finditer(text):
        # Everything BEFORE this token — escape as plain text.
        parts.append(str(escape(text[last:match.start()])))
        name = str(escape(match.group(1)))
        uid = int(match.group(2))
        parts.append(
            f'<a href="/tasks/?scope=employees&amp;user_id={uid}" '
            f'class="mention" '
            f'style="color:#059669;font-weight:600;'
            f'background:#ECFDF5;padding:1px 6px;border-radius:6px;'
            f'text-decoration:none;">@{name}</a>'
        )
        last = match.end()
    # Tail — everything AFTER the last token.
    parts.append(str(escape(text[last:])))
    return Markup("".join(parts))
