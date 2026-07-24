"""MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24) — Manasty side.

Cross-tenant read + reply surface for support agents. Gated by
@support_agent_required (checks Manasty membership + the narrow
`support.manage_tickets` permission). Explicitly NOT super-admin.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    abort, g,
)
from flask_login import login_required, current_user
from app import db
from app.models import (
    SupportTicket, SupportTicketComment, SupportTicketAudit,
    Company, User,
    STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_WAITING,
    STATUS_RESOLVED, STATUS_CLOSED,
    ALL_STATUSES, ALL_PRIORITIES,
    STATUS_LABELS_AR, PRIORITY_LABELS_AR,
    ACTION_REPLY, ACTION_INTERNAL, ACTION_STATUS,
    ACTION_PRIORITY, ACTION_ASSIGN,
)
from app.services.support_permissions import (
    support_agent_required, save_attachment, SupportAttachmentError,
)


bp = Blueprint("support_admin", __name__)


@bp.route("/")
@login_required
@support_agent_required
def index():
    q = SupportTicket.query
    status = (request.args.get("status") or "").upper()
    if status in ALL_STATUSES:
        q = q.filter(SupportTicket.status == status)
    priority = (request.args.get("priority") or "").upper()
    if priority in ALL_PRIORITIES:
        q = q.filter(SupportTicket.priority == priority)
    company_id = request.args.get("company_id", type=int)
    if company_id:
        q = q.filter(SupportTicket.company_id == company_id)

    rows = q.order_by(SupportTicket.created_at.desc()).all()
    # Companies with any ticket — for the filter dropdown.
    companies = Company.query.filter(
        Company.id.in_(db.session.query(SupportTicket.company_id).distinct())
    ).all()
    return render_template(
        "admin/support/index.html", rows=rows, companies=companies,
        status_labels=STATUS_LABELS_AR,
        priority_labels=PRIORITY_LABELS_AR,
        all_statuses=ALL_STATUSES, all_priorities=ALL_PRIORITIES,
    )


@bp.route("/<int:ticket_id>")
@login_required
@support_agent_required
def detail(ticket_id):
    t = db.session.get(SupportTicket, ticket_id) or abort(404)
    return render_template(
        "admin/support/detail.html", ticket=t,
        status_labels=STATUS_LABELS_AR,
        priority_labels=PRIORITY_LABELS_AR,
        all_statuses=ALL_STATUSES, all_priorities=ALL_PRIORITIES,
    )


@bp.route("/<int:ticket_id>/reply", methods=["POST"])
@login_required
@support_agent_required
def reply(ticket_id):
    t = db.session.get(SupportTicket, ticket_id) or abort(404)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("اكتب رد قبل الإرسال", "error")
        return redirect(url_for("support_admin.detail",
                                  ticket_id=t.id))
    row = SupportTicketComment(
        ticket_id=t.id, company_id=t.company_id,
        user_id=current_user.id, content=content,
        is_internal=False,
    )
    try:
        key, name = save_attachment(request.files.get("file"), t.id)
        if key:
            row.attachment_url = key
            row.attachment_name = name
    except SupportAttachmentError as e:
        flash(str(e), "warning")
    db.session.add(row)
    db.session.add(SupportTicketAudit(
        ticket_id=t.id, actor_id=current_user.id,
        action=ACTION_REPLY, new_value=content[:200],
    ))
    # Auto-flip status OPEN → IN_PROGRESS when support starts replying.
    if t.status == STATUS_OPEN:
        old = t.status
        t.status = STATUS_IN_PROGRESS
        db.session.add(SupportTicketAudit(
            ticket_id=t.id, actor_id=current_user.id,
            action=ACTION_STATUS, old_value=old, new_value=t.status,
        ))
    db.session.commit()
    _notify_customer_of_reply(t)
    flash("تم إرسال الرد", "success")
    return redirect(url_for("support_admin.detail", ticket_id=t.id))


@bp.route("/<int:ticket_id>/internal-note", methods=["POST"])
@login_required
@support_agent_required
def internal_note(ticket_id):
    t = db.session.get(SupportTicket, ticket_id) or abort(404)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("اكتب الملاحظة قبل الحفظ", "error")
        return redirect(url_for("support_admin.detail",
                                  ticket_id=t.id))
    db.session.add(SupportTicketComment(
        ticket_id=t.id, company_id=t.company_id,
        user_id=current_user.id, content=content, is_internal=True,
    ))
    db.session.add(SupportTicketAudit(
        ticket_id=t.id, actor_id=current_user.id,
        action=ACTION_INTERNAL, new_value=content[:200],
    ))
    db.session.commit()
    flash("تمت إضافة الملاحظة الداخلية", "success")
    return redirect(url_for("support_admin.detail", ticket_id=t.id))


@bp.route("/<int:ticket_id>/status", methods=["POST"])
@login_required
@support_agent_required
def status(ticket_id):
    t = db.session.get(SupportTicket, ticket_id) or abort(404)
    new_status = (request.form.get("status") or "").strip().upper()
    new_priority = (request.form.get("priority") or "").strip().upper()
    assigned_raw = request.form.get("assigned_to_id")

    if new_status in ALL_STATUSES and new_status != t.status:
        db.session.add(SupportTicketAudit(
            ticket_id=t.id, actor_id=current_user.id,
            action=ACTION_STATUS,
            old_value=t.status, new_value=new_status,
        ))
        t.status = new_status
        if new_status == STATUS_RESOLVED:
            t.resolved_at = datetime.utcnow()

    if new_priority in ALL_PRIORITIES and new_priority != t.priority:
        db.session.add(SupportTicketAudit(
            ticket_id=t.id, actor_id=current_user.id,
            action=ACTION_PRIORITY,
            old_value=t.priority, new_value=new_priority,
        ))
        t.priority = new_priority

    if assigned_raw is not None:
        try:
            new_assignee = int(assigned_raw) if assigned_raw else None
        except (TypeError, ValueError):
            new_assignee = None
        if new_assignee != t.assigned_to_id:
            db.session.add(SupportTicketAudit(
                ticket_id=t.id, actor_id=current_user.id,
                action=ACTION_ASSIGN,
                old_value=str(t.assigned_to_id or "-"),
                new_value=str(new_assignee or "-"),
            ))
            t.assigned_to_id = new_assignee

    db.session.commit()
    flash("تم تحديث التذكرة", "success")
    return redirect(url_for("support_admin.detail", ticket_id=t.id))


def _notify_customer_of_reply(t):
    try:
        from app.services.opsflow_extras import notify_users
        from app.models import NotificationKind
        notify_users(
            [t.created_by_id],
            company_id=t.company_id,
            kind=NotificationKind.NEW_LEAD.value,
            title=f"رد جديد من الدعم — تذكرة #{t.id}",
            body=t.title,
            link_url=url_for("support.detail", ticket_id=t.id,
                              _external=False),
        )
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "support-admin reply notify failed for ticket %s", t.id)
