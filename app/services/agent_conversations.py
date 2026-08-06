"""MARSOUD-AGENT-MEMORY-05 (2026-08-06) — conversation lifecycle
helpers for the agent chat.

Three surfaces:

  1. get_or_create_current_conversation(user_id, company_id,
     agent_type) — the chat route calls this when no conversation_id
     came in the body. Returns the user's most recent OPEN
     (non-archived) conversation for that (user, company,
     agent_type), or creates a fresh one.

  2. list_conversations_for(user_id, company_id, agent_type) —
     sidebar feed. Non-archived rows, newest first.

  3. expire_old_conversations() — cron job. Reads the
     PlatformSetting agent_conversation_retention_days and
     hard-deletes conversations + their messages where
     last_message_at is older than N days. Setting = 0 means
     "never expire" (deliberate; a fat-fingered 0 must not wipe
     every conversation on the next tick).

Everything filters by (company, user, agent_type) — same axis
AgentMessage uses. Cross-user leaks are impossible if the caller
sticks to these helpers rather than raw queries.
"""
from datetime import datetime, timedelta

from app import db


def get_or_create_current_conversation(user_id, company_id,
                                        agent_type="accountant"):
    """Return the user's most recent OPEN conversation for that
    agent_type, or create one. Never crosses users."""
    from app.models import AgentConversation
    conv = (AgentConversation.query
            .filter_by(user_id=user_id, company_id=company_id,
                       agent_type=agent_type, is_archived=False)
            .order_by(AgentConversation.last_message_at.desc())
            .first())
    if conv is not None:
        return conv
    conv = AgentConversation(
        user_id=user_id, company_id=company_id,
        agent_type=agent_type,
        title=None,   # filled in from the first user message
    )
    db.session.add(conv)
    db.session.commit()
    return conv


def create_conversation(user_id, company_id,
                         agent_type="accountant"):
    """Force-create a new conversation. Used by the '+ محادثة جديدة'
    button."""
    from app.models import AgentConversation
    conv = AgentConversation(
        user_id=user_id, company_id=company_id,
        agent_type=agent_type, title=None,
    )
    db.session.add(conv)
    db.session.commit()
    return conv


def list_conversations_for(user_id, company_id,
                            agent_type="accountant",
                            limit=50):
    """Sidebar feed. Non-archived only, newest first."""
    from app.models import AgentConversation
    q = (AgentConversation.query
         .filter_by(user_id=user_id, company_id=company_id,
                    agent_type=agent_type, is_archived=False)
         .order_by(AgentConversation.last_message_at.desc())
         .limit(limit))
    return q.all()


def touch_conversation(conv, first_user_text=None):
    """Update last_message_at + set the title from the first user
    message if it's still empty."""
    conv.last_message_at = datetime.utcnow()
    if not (conv.title or "").strip() and first_user_text:
        conv.title = first_user_text.strip()[:60]
    db.session.commit()


def archive_conversation(conv):
    """Sidebar-delete. Row + messages linger until the cron sweep."""
    conv.is_archived = True
    db.session.commit()


# ─── Retention (cron) ──────────────────────────────────────────────────
def retention_days():
    """Read the super-admin setting. Default 90, floor 0.
    0 means "never expire" (deliberate — a fat-fingered 0 must not
    wipe every user's history)."""
    from app.models.platform_setting import PlatformSetting
    row = PlatformSetting.query.filter_by(
        key="agent_conversation_retention_days").first()
    if row is None:
        return 90
    try:
        n = int((row.value or "").strip())
    except (TypeError, ValueError):
        return 90
    return max(0, n)


def expire_old_conversations():
    """Cron entry: hard-delete conversations older than the retention
    window, along with their messages. Retention=0 → skip entirely.

    Returns a summary dict for the cron tick response."""
    from app.models import AgentConversation, AgentMessage
    days = retention_days()
    if days <= 0:
        return {"skipped": "retention=0 disables expiry"}
    cutoff = datetime.utcnow() - timedelta(days=days)
    stale = AgentConversation.query.filter(
        AgentConversation.last_message_at < cutoff).all()
    if not stale:
        return {"deleted_conversations": 0,
                "deleted_messages": 0,
                "retention_days": days}
    stale_ids = [c.id for c in stale]
    msgs = AgentMessage.query.filter(
        AgentMessage.conversation_id.in_(stale_ids)).delete(
        synchronize_session=False)
    n = AgentConversation.query.filter(
        AgentConversation.id.in_(stale_ids)).delete(
        synchronize_session=False)
    db.session.commit()
    return {"deleted_conversations": n,
            "deleted_messages": msgs,
            "retention_days": days}
