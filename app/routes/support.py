"""MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24) — customer.

Standard company_id-scoped routes. A customer company sees ONLY
its own tickets. The cross-tenant admin surface lives in
app/routes/support_admin.py, gated by @support_agent_required.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    abort, g, send_file,
)
from flask_login import login_required, current_user
from app import db
from app.models import (
    SupportTicket, SupportTicketComment,
    STATUS_OPEN, ALL_PRIORITIES, PRIORITY_MEDIUM,
    STATUS_LABELS_AR, PRIORITY_LABELS_AR,
)
from app.services.support_permissions import (
    save_attachment, read_attachment_path, SupportAttachmentError,
    is_support_agent,
)


bp = Blueprint("support", __name__)


def _ticket_for_company(ticket_id):
    """404 unless the ticket belongs to the active company. Support
    agents can view any ticket via /support-admin — this endpoint is
    strictly per-company."""
    t = db.session.get(SupportTicket, ticket_id)
    if not t or not g.active_company or t.company_id != g.active_company.id:
        abort(404)
    return t


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("dashboard.index"))
    rows = SupportTicket.query.filter_by(
        company_id=g.active_company.id
    ).order_by(SupportTicket.created_at.desc()).all()
    return render_template(
        "support/index.html", tickets=rows,
        status_labels=STATUS_LABELS_AR,
        priority_labels=PRIORITY_LABELS_AR,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not g.active_company:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        priority = (request.form.get("priority") or "").strip().upper()
        if priority not in ALL_PRIORITIES:
            priority = PRIORITY_MEDIUM
        if not title:
            flash("العنوان مطلوب", "error")
            return redirect(url_for("support.new"))
        t = SupportTicket(
            company_id=g.active_company.id,
            created_by_id=current_user.id,
            title=title[:200],
            description=description or None,
            priority=priority, status=STATUS_OPEN,
        )
        db.session.add(t); db.session.flush()
        # Optional first attachment.
        try:
            key, name = save_attachment(request.files.get("file"), t.id)
            if key:
                db.session.add(SupportTicketComment(
                    ticket_id=t.id, company_id=t.company_id,
                    user_id=current_user.id,
                    content="(مرفق أولي)",
                    attachment_url=key, attachment_name=name,
                    is_internal=False,
                ))
        except SupportAttachmentError as e:
            flash(str(e), "warning")
        db.session.commit()
        _notify_support_of_new_ticket(t)
        flash("تم إرسال تذكرة الدعم — سيتم الرد قريباً.", "success")
        return redirect(url_for("support.detail", ticket_id=t.id))
    return render_template("support/new.html",
                             priority_labels=PRIORITY_LABELS_AR)


@bp.route("/<int:ticket_id>")
@login_required
def detail(ticket_id):
    t = _ticket_for_company(ticket_id)
    # Filter out internal notes for the customer view.
    visible = [c for c in t.comments if not c.is_internal]
    return render_template(
        "support/detail.html", ticket=t, comments=visible,
        status_labels=STATUS_LABELS_AR,
        priority_labels=PRIORITY_LABELS_AR,
    )


@bp.route("/<int:ticket_id>/comment", methods=["POST"])
@login_required
def comment(ticket_id):
    t = _ticket_for_company(ticket_id)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("اكتب رسالة قبل الإرسال", "error")
        return redirect(url_for("support.detail", ticket_id=t.id))
    row = SupportTicketComment(
        ticket_id=t.id, company_id=t.company_id,
        user_id=current_user.id, content=content, is_internal=False,
    )
    try:
        key, name = save_attachment(request.files.get("file"), t.id)
        if key:
            row.attachment_url = key
            row.attachment_name = name
    except SupportAttachmentError as e:
        flash(str(e), "warning")
    db.session.add(row); db.session.commit()
    _notify_support_of_reply(t, from_customer=True)
    flash("تم إرسال ردك", "success")
    return redirect(url_for("support.detail", ticket_id=t.id))


@bp.route("/attachments/<path:key>")
@login_required
def attachment(key):
    """Stream a support attachment. Auth = the file's ticket must
    belong to the active company OR the user must be a support
    agent."""
    ticket_id = key.split("/", 1)[0] if "/" in key else None
    if ticket_id is None:
        abort(404)
    try:
        t = db.session.get(SupportTicket, int(ticket_id))
    except (TypeError, ValueError):
        abort(404)
    if not t:
        abort(404)
    if t.company_id != (g.active_company.id if g.active_company else 0):
        if not is_support_agent():
            abort(404)
    p = read_attachment_path(key)
    if p is None:
        abort(404)
    resp = send_file(str(p), as_attachment=False)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _notify_support_of_new_ticket(t):
    """Ping every Manasty support_agent + owner about a new ticket."""
    try:
        from app.services.opsflow_extras import notify_users
        from app.services.manasty import manasty_owner_ids
        from app.models import NotificationKind
        notify_users(
            manasty_owner_ids(),
            company_id=t.company_id,
            kind=NotificationKind.NEW_LEAD.value,   # reuse; no NEW_SUPPORT enum yet
            title=f"🆘 تذكرة دعم جديدة #{t.id}",
            body=t.title,
            link_url=url_for("support_admin.detail",
                              ticket_id=t.id, _external=False),
        )
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "support-new notify failed for ticket %s", t.id)


def _notify_support_of_reply(t, from_customer):
    """Customer replied → notify support. Support replied → notify
    the ticket creator."""
    try:
        from app.services.opsflow_extras import notify_users
        from app.services.manasty import manasty_owner_ids
        from app.models import NotificationKind
        if from_customer:
            recipients = manasty_owner_ids()
            company_id = t.company_id
            link = url_for("support_admin.detail",
                            ticket_id=t.id, _external=False)
        else:
            recipients = [t.created_by_id]
            company_id = t.company_id
            link = url_for("support.detail",
                            ticket_id=t.id, _external=False)
        notify_users(
            recipients, company_id=company_id,
            kind=NotificationKind.NEW_LEAD.value,
            title=f"رد جديد على تذكرة #{t.id}",
            body=t.title, link_url=link,
        )
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "support-reply notify failed for ticket %s", t.id)
