"""MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22).

Filter a target audience + fan out one Notification per recipient
+ optionally an email per recipient. Sending is idempotent per
broadcast — Broadcast.sent_at guards against re-sends.
"""
from datetime import datetime
from app import db
from app.models import (
    User, Company, Notification, NotificationKind,
    AUDIENCE_ALL, AUDIENCE_TRIAL, AUDIENCE_ACTIVE,
    AUDIENCE_EXPIRED, AUDIENCE_BY_PLAN,
)
from app.models.user import user_companies


class BroadcastError(Exception):
    """Raised when a broadcast can't be sent (already sent, empty
    audience, etc.)."""


def audience_query(filter_dict):
    """Build a User query that matches the audience filter. Returns
    only ACTIVE users so we don't spam disabled / pending accounts.

    Filter shapes:
      · {"kind": "all"}
      · {"kind": "trial"}          companies still inside subscription window
      · {"kind": "active"}         companies with non-expired subscription
      · {"kind": "expired"}        companies past subscription_expires_at
      · {"kind": "by_plan", "plan_id": N}
    """
    kind = (filter_dict or {}).get("kind", AUDIENCE_ALL)
    q = User.query.filter(User.is_active == True).filter(
        User.is_superadmin == False)

    if kind == AUDIENCE_ALL:
        return q

    now = datetime.utcnow()
    # All the company-scoped filters need to join through user_companies.
    company_join = db.session.query(user_companies.c.user_id).join(
        Company, Company.id == user_companies.c.company_id
    )

    if kind == AUDIENCE_TRIAL:
        cid_sub = company_join.filter(
            Company.subscription_started_at != None,
            Company.subscription_expires_at > now,
            # No paid plan attached yet → still trialing.
            Company.intended_plan_id == None,
        ).subquery()
        return q.filter(User.id.in_(cid_sub))
    if kind == AUDIENCE_ACTIVE:
        cid_sub = company_join.filter(
            Company.subscription_expires_at > now,
        ).subquery()
        return q.filter(User.id.in_(cid_sub))
    if kind == AUDIENCE_EXPIRED:
        cid_sub = company_join.filter(
            Company.subscription_expires_at < now,
        ).subquery()
        return q.filter(User.id.in_(cid_sub))
    if kind == AUDIENCE_BY_PLAN:
        plan_id = int(filter_dict.get("plan_id") or 0)
        if not plan_id:
            return q.filter(User.id == 0)   # empty
        cid_sub = company_join.filter(
            Company.plan_id == plan_id,
        ).subquery()
        return q.filter(User.id.in_(cid_sub))
    # Unknown kind → empty audience (safer than silently spamming
    # everyone).
    return q.filter(User.id == 0)


def preview_count(filter_dict):
    return audience_query(filter_dict).count()


def send(broadcast):
    """Fan out the broadcast. Returns (sent, failed_emails)."""
    if broadcast.sent_at is not None:
        raise BroadcastError("هذه الرسالة تم إرسالها بالفعل.")
    from app.services.email import send_email

    channels = set(broadcast.channel_list)
    audience = audience_query(broadcast.audience)
    users = audience.all()

    sent = 0
    failed = 0
    for u in users:
        # Notification.company_id is NOT NULL — skip users without
        # any company (super-admins, orphan accounts). Email still
        # goes out to them below if EMAIL is enabled.
        cid = u.companies[0].id if u.companies else None
        if cid is not None:
            try:
                db.session.add(Notification(
                    user_id=u.id, company_id=cid,
                    kind=NotificationKind.BROADCAST.value,
                    title=broadcast.title,
                    body=_strip_html(broadcast.body_html),
                    link_url=None,
                ))
            except Exception:
                failed += 1
                continue

        if "EMAIL" in channels:
            try:
                send_email(u.email, broadcast.title,
                            broadcast.body_html)
            except Exception:
                failed += 1
        sent += 1

    broadcast.sent_at = datetime.utcnow()
    broadcast.target_count = sent
    db.session.commit()
    return sent, failed


def _strip_html(html):
    """Very cheap HTML → text for the Notification body preview.
    The full HTML lives in the email; the in-app notification just
    needs a plain-text version to render safely in a card."""
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]
