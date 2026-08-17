"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — signup
rejection logging + auto-learned domain blocklist.

Four entry points:

  · `record_rejection(reason, email, ip_address)` — persist
    one row per rejected signup. Called from every gate in
    `app/routes/auth.py::register`. For honeypot rejections
    only, also fires `maybe_auto_block`.

  · `maybe_auto_block(domain)` — after every honeypot log,
    checks the rolling 24-hour window and adds the domain
    to `blocked_domains` if it hit the threshold (3) AND
    isn't in the whitelist AND isn't already blocked.

  · `is_domain_blocked(email)` — O(1) hot-path check called
    BEFORE the existing bot_guard gates. A blocked email
    domain gets the soft-success decoy (same as honeypot)
    so a bot cannot distinguish "domain is blocked" from
    "form was submitted".

  · `unblock_domain(domain, actor_id)` — human review
    escape hatch from /admin/rejected-signups. Idempotent.

Design invariants:

  1. Only **honeypot** rejections drive auto-block. Rate-
     limit trips catch over-eager legit users; turnstile
     trips can be network flakes; spam-domain trips are
     already deterministic (static list). Honeypot is the
     one gate where a trip is a near-certain bot signal.

  2. `WHITELISTED_DOMAINS` is HARD-CODED and frozen. A bot
     with a burner gmail account cannot lock out every
     legitimate gmail user. Making this configurable via
     a dashboard would be dangerous.

  3. Every DB write is best-effort. A hiccup on the log
     table must never DoS the signup form — the try/except
     catches everything, logs to app.logger, and returns.
"""
from datetime import datetime, timedelta

from flask import current_app

from app import db
from app.models import BlockedDomain, BlockedEmail, SignupRejection


# ── constants ────────────────────────────────────────────── #

# Frozen — auto-block NEVER touches these. See invariant 2.
WHITELISTED_DOMAINS = frozenset({
    "gmail.com", "outlook.com", "hotmail.com",
    "yahoo.com", "icloud.com",
    # Regional variants that see heavy legit use in the
    # Arab market. Add sparingly.
    "hotmail.co.uk", "yahoo.co.uk",
    "live.com", "msn.com",
})

# Auto-block trigger: N honeypot rejections in a rolling
# window of H hours from the same domain → auto-block.
HONEYPOT_TRIGGER_COUNT = 3
HONEYPOT_TRIGGER_WINDOW_HOURS = 24


def _extract_domain(email):
    """Lower-case, whitespace-stripped domain, or None."""
    if not email or "@" not in email:
        return None
    d = email.rsplit("@", 1)[-1].strip().lower()
    return d or None


# ── public API ───────────────────────────────────────────── #

def record_rejection(reason, email, ip_address=None,
                     honeypot_value=None):
    """Persist one rejection row + fire auto-block check
    for honeypot trips.

    reason ∈ {"honeypot", "rate_limit", "spam_domain",
              "turnstile", "blocked_domain", "blocked_email",
              "bot_immediate"}.

    Never raises. If the log write fails, we swallow the
    exception, log to app.logger, and return — bot
    protection must not become a signup outage.

    MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17.
    Now persists the full email + the raw honeypot payload so
    the super-admin can see exactly what the bot injected. Both
    columns are nullable to keep the write best-effort.
    """
    domain = _extract_domain(email) or "(unknown)"
    normalized_email = (email or "").strip().lower() or None
    hp_val = (honeypot_value or "").strip() or None
    try:
        db.session.add(SignupRejection(
            email_domain=domain,
            email=normalized_email,
            honeypot_value=hp_val,
            reason=reason,
            ip_address=ip_address))
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        try:
            current_app.logger.exception(
                "signup_rejection log failed "
                "(reason=%s, domain=%s)", reason, domain)
        except Exception:
            pass
        return
    if reason == "honeypot":
        try:
            maybe_auto_block(domain)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            try:
                current_app.logger.exception(
                    "maybe_auto_block failed for %s", domain)
            except Exception:
                pass


def maybe_auto_block(domain):
    """After every honeypot rejection, add `domain` to
    `blocked_domains` IFF:
      · not in WHITELISTED_DOMAINS AND
      · not already actively blocked AND
      · ≥ HONEYPOT_TRIGGER_COUNT honeypot rejections in the
        last HONEYPOT_TRIGGER_WINDOW_HOURS hours.

    Returns True when a new BlockedDomain row was added,
    False otherwise. Also writes a `signup_domain_auto_blocked`
    PlatformAuditLog entry on block.
    """
    if not domain or domain == "(unknown)":
        return False
    if domain in WHITELISTED_DOMAINS:
        return False
    existing = BlockedDomain.query.filter_by(
        domain=domain, is_active=True).first()
    if existing:
        return False
    cutoff = (datetime.utcnow()
              - timedelta(hours=HONEYPOT_TRIGGER_WINDOW_HOURS))
    n = SignupRejection.query.filter(
        SignupRejection.email_domain == domain,
        SignupRejection.reason == "honeypot",
        SignupRejection.created_at >= cutoff,
    ).count()
    if n < HONEYPOT_TRIGGER_COUNT:
        return False
    row = BlockedDomain(
        domain=domain, is_active=True,
        reason=(f"auto: {n} honeypot rejections in "
                f"{HONEYPOT_TRIGGER_WINDOW_HOURS}h"),
    )
    db.session.add(row)
    db.session.commit()
    # Best-effort audit log — this stays inside the try
    # already wrapping record_rejection's call site, so a
    # PAL write failure won't undo the block.
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "signup_domain_auto_blocked",
            details=f"domain={domain} count_24h={n}",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def is_domain_blocked(email):
    """Hot-path check. Called BEFORE the other bot_guard
    gates in auth.register. Returns True iff the email's
    domain is in an active BlockedDomain row.

    Safe on malformed input — returns False for anything
    without a parseable domain."""
    d = _extract_domain(email)
    if not d:
        return False
    try:
        return BlockedDomain.query.filter_by(
            domain=d, is_active=True).first() is not None
    except Exception:  # noqa: BLE001
        # Same reason as record_rejection — never DoS
        # signup because the log table is unreachable.
        try:
            current_app.logger.exception(
                "is_domain_blocked check failed for %s", d)
        except Exception:
            pass
        return False


# ── MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17 ── #

def block_domain_now(domain, reason, actor_id=None):
    """Immediately add `domain` to `blocked_domains` — bypasses
    the 3-in-24h counter used by `maybe_auto_block`.

    TKT-17: when a bot signup is detected (honeypot trip), block
    the domain IMMEDIATELY if it's outside `WHITELISTED_DOMAINS`.
    We already know it's a bot; there's no need to wait for a
    counter to trip. Whitelisted domains are still exempt — a
    burner-gmail bot must not lock out all real gmail users.

    Returns True when a new row was added, False when the
    domain was whitelisted, already blocked, or malformed.
    Idempotent + safe on the "already blocked" case.
    """
    if not domain:
        return False
    d = domain.strip().lower()
    if not d or d == "(unknown)":
        return False
    if d in WHITELISTED_DOMAINS:
        return False
    existing = BlockedDomain.query.filter_by(
        domain=d, is_active=True).first()
    if existing:
        return False
    row = BlockedDomain(
        domain=d, is_active=True,
        reason=reason or "immediate block on bot detection",
    )
    db.session.add(row)
    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "signup_domain_immediate_blocked",
            actor_id=actor_id,
            details=f"domain={d}, reason={reason}",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def block_email(email, reason, actor_id=None):
    """Permanently block a specific email address. Sibling of
    `block_domain_now` — used by TKT-17's honeypot path to lock
    out the exact address that submitted, even on whitelisted
    domains where the domain itself must stay unblocked.

    Idempotent — a second call for an already-active row is a
    no-op and returns False. Returns True on the first insert.
    Never raises — write failures are swallowed with a logger
    entry so the signup form never 500s on a log-table hiccup.
    """
    if not email:
        return False
    e = email.strip().lower()
    if not e or "@" not in e:
        return False
    try:
        existing = BlockedEmail.query.filter_by(
            email=e, is_active=True).first()
        if existing:
            return False
        row = BlockedEmail(
            email=e, is_active=True,
            reason=reason or "immediate block on bot detection",
        )
        db.session.add(row)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        try:
            current_app.logger.exception(
                "block_email failed for %s", e)
        except Exception:
            pass
        return False
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "signup_email_blocked",
            actor_id=actor_id,
            details=f"email={e}, reason={reason}",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def is_email_blocked(email):
    """Hot-path check. Called BEFORE the other bot_guard gates
    in `auth.register`. Returns True iff the email is in an
    active BlockedEmail row.

    Safe on malformed input — returns False for anything without
    an `@` or where the log-table read fails (never DoS signup
    because the log table is unreachable).
    """
    if not email:
        return False
    e = email.strip().lower()
    if not e or "@" not in e:
        return False
    try:
        return BlockedEmail.query.filter_by(
            email=e, is_active=True).first() is not None
    except Exception:  # noqa: BLE001
        try:
            current_app.logger.exception(
                "is_email_blocked check failed for %s", e)
        except Exception:
            pass
        return False


def unblock_email(email, actor_id):
    """Superadmin escape hatch — lift a false-positive email
    block. Idempotent: returns False when the email isn't
    currently blocked.
    """
    if not email:
        return False
    e = email.strip().lower()
    row = BlockedEmail.query.filter_by(
        email=e, is_active=True).first()
    if not row:
        return False
    row.is_active = False
    row.unblocked_at = datetime.utcnow()
    row.unblocked_by_id = actor_id
    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "signup_email_unblocked",
            actor_id=actor_id,
            details=f"email={e}",
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def unblock_domain(domain, actor_id):
    """Superadmin action from /admin/rejected-signups.
    Lifts an auto-block after human review. Idempotent —
    returns False if the domain isn't currently blocked.
    """
    if not domain:
        return False
    d = domain.strip().lower()
    row = BlockedDomain.query.filter_by(
        domain=d, is_active=True).first()
    if not row:
        return False
    row.is_active = False
    row.unblocked_at = datetime.utcnow()
    row.unblocked_by_id = actor_id
    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "signup_domain_unblocked",
            actor_id=actor_id,
            details=f"domain={d}",
        )
    except Exception:  # noqa: BLE001
        pass
    return True
