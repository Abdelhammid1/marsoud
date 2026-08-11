"""Routes for the Cycle 7 gap-close — documents, notifications, audit,
feedback (internal), client portal, per-customer projects view.

This module registers several small blueprints:
    documents_bp at /docs
    notifications_bp at /notifications
    audit_bp at /audit
    portal_bp at /portal
"""
from datetime import datetime
import json
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
    send_from_directory, current_app,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Document, DocumentSourceType, DocumentVisibility,
    Notification, NotificationKind,
    AuditEntry,
    ClientFeedback,
    Lead, Project, ProjectStatus, ProjectMember, Task, TaskStatus, KANBAN_ORDER,
    User, Customer,
)
from app.models.user import user_companies
from app.services.opsflow_extras import (
    save_document, delete_document, documents_for, DocumentError,
    submit_client_feedback, FeedbackError,
    notify, mark_notification_read,
)
from app.services.permissions import (
    require_permission, get_user_role,
)


# ─── Documents (generic) ────────────────────────────────────────────────
documents_bp = Blueprint("documents", __name__)


def _can_attach_to(source_type, source_id, company_id):
    """Visibility check: the current user must be allowed to see the parent
    entity before uploading / deleting docs."""
    role = get_user_role(current_user.id, company_id) if g.active_company else None
    if source_type == "LEAD":
        lead = db.session.get(Lead, source_id)
        if not lead or lead.company_id != company_id:
            return False
        if role in ("owner", "admin", "sales_manager"):
            return True
        if role == "sales_rep" and lead.assigned_to_id == current_user.id:
            return True
        return False
    if source_type == "PROJECT":
        p = db.session.get(Project, source_id)
        if not p or p.company_id != company_id:
            return False
        if role in ("owner", "admin"):
            return True
        if role == "project_manager" and p.manager_id == current_user.id:
            return True
        if role == "team_member":
            return any(m.user_id == current_user.id for m in p.members)
        return False
    if source_type == "TASK":
        t = db.session.get(Task, source_id)
        if not t or t.company_id != company_id:
            return False
        if role in ("owner", "admin"):
            return True
        if role == "project_manager" and t.project.manager_id == current_user.id:
            return True
        return t.assigned_to_id == current_user.id
    if source_type == "CASH_CUSTODY_SETTLEMENT":
        # MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — source_id
        # is the CashCustodySettlementLine.id. Custody-managing roles
        # can always attach; the custody's holder-employee can attach
        # to their own custody's lines.
        from app.models import CashCustodySettlementLine, CustodyHolderType
        line = db.session.get(CashCustodySettlementLine, source_id)
        if not line or line.company_id != company_id:
            return False
        if role in ("owner", "admin", "accountant"):
            return True
        # Employee side: only the holder-employee's own custody.
        custody = line.custody
        if (custody
                and custody.holder_type == CustodyHolderType.EMPLOYEE
                and custody.employee
                and custody.employee.user_id == current_user.id):
            return True
        return False
    if source_type == "ITEM_CUSTODY":
        # MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — source_id is the
        # ItemCustody.id. Custody-managing roles always allowed;
        # the holder-employee can attach handover/return photos
        # to their own custody row.
        from app.models import ItemCustody, CustodyHolderType
        custody = db.session.get(ItemCustody, source_id)
        if not custody or custody.company_id != company_id:
            return False
        if role in ("owner", "admin", "accountant"):
            return True
        if (custody.holder_type == CustodyHolderType.EMPLOYEE
                and custody.employee
                and custody.employee.user_id == current_user.id):
            return True
        return False
    if source_type == "CASH_CUSTODY_REQUEST":
        # MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) —
        # source_id is the CashCustodyRequest.id. Same auth
        # shape as the two custody types above: custody-
        # managing roles always allowed; the request's
        # holder-employee can also attach (so they can add
        # supporting proof from their portal side if the
        # workflow evolves that way).
        from app.models import CashCustodyRequest, CustodyHolderType
        req = db.session.get(CashCustodyRequest, source_id)
        if not req or req.company_id != company_id:
            return False
        if role in ("owner", "admin", "accountant"):
            return True
        if (req.holder_type == CustodyHolderType.EMPLOYEE
                and req.employee
                and req.employee.user_id == current_user.id):
            return True
        return False
    return False


@documents_bp.route("/upload/<source_type>/<int:source_id>", methods=["POST"])
@login_required
def upload(source_type, source_id):
    cid = g.active_company.id
    if not _can_attach_to(source_type, source_id, cid):
        abort(403)
    visibility = request.form.get("visibility", "INTERNAL")
    if visibility not in ("INTERNAL", "CLIENT"):
        visibility = "INTERNAL"
    try:
        save_document(
            company_id=cid,
            source_type=source_type,
            source_id=source_id,
            file_storage=request.files.get("file"),
            visibility=visibility,
            uploaded_by_id=current_user.id,
        )
        flash("تم رفع الملف", "success")
    except DocumentError as e:
        flash(str(e), "error")
    # Bounce back to the parent entity
    target = None
    if source_type == "LEAD":
        target = url_for("leads.detail", lead_id=source_id)
    elif source_type == "PROJECT":
        target = url_for("projects.detail", project_id=source_id)
    elif source_type == "TASK":
        target = url_for("tasks.detail", task_id=source_id)
    elif source_type == "CASH_CUSTODY_SETTLEMENT":
        # Bounce to the parent custody's detail page.
        from app.models import CashCustodySettlementLine
        line = db.session.get(CashCustodySettlementLine, source_id)
        if line and line.custody_id:
            target = url_for("custody.detail",
                             custody_id=line.custody_id)
    elif source_type == "ITEM_CUSTODY":
        target = url_for("item_custody.detail",
                         custody_id=source_id)
    elif source_type == "CASH_CUSTODY_REQUEST":
        # MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) —
        # no per-request detail page today, so bounce back
        # to the requests list (same place reject lands).
        target = url_for("custody.requests")
    return redirect(target or url_for("dashboard.index"))


@documents_bp.route("/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete(doc_id):
    doc = db.session.get(Document, doc_id)
    if not doc or doc.company_id != g.active_company.id:
        abort(404)
    if not _can_attach_to(doc.source_type, doc.source_id, doc.company_id):
        abort(403)
    delete_document(doc)
    flash("تم حذف الملف", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


# ─── Notifications (bell icon page) ─────────────────────────────────────
# MARSOUD-NOTIF-TENANT-FIX (Abdelhamid 2026-07-15) — a user with
# memberships in multiple companies was seeing notifications from the
# OTHER company leak into the current company's UI. Root cause: all 4
# endpoints filtered only by user_id, ignoring g.active_company. Every
# query below is now scoped to (user_id, company_id) so the bell +
# dropdown + read + read-all only ever touch the active tenant's
# notification set.
notifications_bp = Blueprint("notifications", __name__)


def _active_company_id():
    """Return the currently-active company id or None. Callers use
    None to short-circuit to an empty result set (defense in depth —
    a request with no active company shouldn't see any notification)."""
    from flask import g
    c = g.get("active_company")
    return c.id if c else None


@notifications_bp.route("/")
@login_required
def index():
    # MARSOUD-NOTIF-FILTER (Abdelhamid image #17) — allow the user to
    # narrow the list to unread only via ?filter=unread. Anything else
    # (missing / "all" / a typo) shows everything so the URL is safe
    # to bookmark.
    filter_arg = (request.args.get("filter") or "all").strip().lower()
    filter_mode = "unread" if filter_arg == "unread" else "all"
    cid = _active_company_id()
    if cid is None:
        return render_template(
            "notifications/index.html", notifications=[],
            filter_mode=filter_mode, unread_count=0)
    q = Notification.query.filter_by(
        user_id=current_user.id, company_id=cid,
    )
    unread_count = q.filter(Notification.read_at.is_(None)).count()
    if filter_mode == "unread":
        q = q.filter(Notification.read_at.is_(None))
    notifs = q.order_by(Notification.created_at.desc()).limit(200).all()
    return render_template(
        "notifications/index.html", notifications=notifs,
        filter_mode=filter_mode, unread_count=unread_count)


@notifications_bp.route("/dropdown")
@login_required
def dropdown():
    """JSON for the bell dropdown — last 10 + unread count, scoped
    to the ACTIVE company only."""
    from flask import jsonify
    cid = _active_company_id()
    if cid is None:
        return jsonify({"unread": 0, "items": []})
    rows = Notification.query.filter_by(
        user_id=current_user.id, company_id=cid,
    ).order_by(Notification.created_at.desc()).limit(10).all()
    unread = Notification.query.filter_by(
        user_id=current_user.id, company_id=cid, read_at=None,
    ).count()
    return jsonify({
        "unread": unread,
        "items": [{
            "id": n.id,
            "title": n.title,
            "body": (n.body or "")[:120],
            "kind": n.kind,
            "link_url": n.link_url,
            "is_read": n.is_read,
            "created_at": n.created_at.strftime("%Y-%m-%d %H:%M"),
        } for n in rows],
    })


@notifications_bp.route("/<int:n_id>/read", methods=["POST"])
@login_required
def read(n_id):
    cid = _active_company_id()
    n = db.session.get(Notification, n_id)
    # Refuse to mark a notification from a different company as read
    # even if the user technically owns it — matters for the bell
    # scope integrity.
    if (not n or n.user_id != current_user.id
            or (cid is not None and n.company_id != cid)):
        abort(404)
    mark_notification_read(n)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True})
    return redirect(n.link_url or url_for("notifications.index"))


@notifications_bp.route("/read-all", methods=["POST"])
@login_required
def read_all():
    cid = _active_company_id()
    if cid is not None:
        Notification.query.filter_by(
            user_id=current_user.id, company_id=cid, read_at=None,
        ).update({"read_at": datetime.utcnow()})
        db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True})
    return redirect(url_for("notifications.index"))


# ─── Audit log page (admins) ────────────────────────────────────────────
audit_bp = Blueprint("audit_log", __name__)


@audit_bp.route("/")
@login_required
@require_permission("users.view")    # owner/admin
def index():
    cid = g.active_company.id
    entity_filter = (request.args.get("entity") or "").strip()
    action_filter = (request.args.get("action") or "").strip()
    q = AuditEntry.query.filter_by(company_id=cid)
    if entity_filter:
        q = q.filter(AuditEntry.entity_type == entity_filter)
    if action_filter:
        q = q.filter(AuditEntry.action == action_filter)
    entries = q.order_by(AuditEntry.created_at.desc()).limit(500).all()
    # Parse the JSON for display
    for e in entries:
        try:
            e.parsed_changes = json.loads(e.changes_json) if e.changes_json else None
        except (ValueError, TypeError):
            e.parsed_changes = None
    return render_template("audit/index.html",
                           entries=entries,
                           entity_filter=entity_filter,
                           action_filter=action_filter)


# ─── Project feedback (internal — staff approve/reject) ─────────────────
# Lives on the project detail page via a small POST endpoint, plus a
# dedicated approve/reject action for staff.
@audit_bp.route("/projects/<int:project_id>/feedback/<int:fb_id>/approve",
                methods=["POST"])
@login_required
@require_permission("projects.manage")
def feedback_approve(project_id, fb_id):
    p = db.session.get(Project, project_id)
    fb = db.session.get(ClientFeedback, fb_id)
    if not p or not fb or fb.project_id != p.id or p.company_id != g.active_company.id:
        abort(404)
    fb.approved = True
    db.session.commit()
    flash("تم اعتماد ملاحظات العميل — يمكن الآن إغلاق المشروع", "success")
    return redirect(url_for("projects.detail", project_id=p.id))


# ─── Client Portal ──────────────────────────────────────────────────────
portal_bp = Blueprint("portal", __name__)


def _client_or_403():
    """Make sure the current user is an active client tied to a Customer."""
    if not current_user.is_authenticated:
        abort(401)
    if not current_user.linked_customer_id:
        abort(403)
    cust = db.session.get(Customer, current_user.linked_customer_id)
    if not cust:
        abort(403)
    return cust


def _client_projects(customer):
    return Project.query.filter_by(customer_id=customer.id).order_by(
        Project.created_at.desc(),
    ).all()


@portal_bp.route("/")
@login_required
def index():
    cust = _client_or_403()
    projects = _client_projects(cust)
    return render_template("portal/index.html",
                           customer=cust, projects=projects)


@portal_bp.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id):
    cust = _client_or_403()
    p = db.session.get(Project, project_id)
    if not p or p.customer_id != cust.id:
        abort(404)
    p.recompute_progress()
    db.session.commit()

    # Current milestone = first incomplete milestone in order
    current_milestone = None
    for m in p.milestones:
        if not m.is_done:
            current_milestone = m
            break

    # Client-visible documents only
    project_docs = documents_for("PROJECT", p.id, only_client_visible=True)
    # Existing feedback by this client
    existing_feedback = ClientFeedback.query.filter_by(
        project_id=p.id, customer_id=cust.id,
    ).order_by(ClientFeedback.submitted_at.desc()).all()
    can_submit_feedback = (
        p.status in (ProjectStatus.DELIVERED, ProjectStatus.CLIENT_FEEDBACK)
        and not any(fb.approved for fb in existing_feedback)
    )

    return render_template("portal/project.html",
                           customer=cust, project=p,
                           current_milestone=current_milestone,
                           documents=project_docs,
                           feedback_rows=existing_feedback,
                           can_submit_feedback=can_submit_feedback)


@portal_bp.route("/projects/<int:project_id>/feedback", methods=["POST"])
@login_required
def submit_feedback(project_id):
    cust = _client_or_403()
    p = db.session.get(Project, project_id)
    if not p or p.customer_id != cust.id:
        abort(404)
    try:
        rating = int(request.form.get("rating", 0))
        submit_client_feedback(
            p, customer_id=cust.id, rating=rating,
            comment=request.form.get("comment"),
            submitted_by_user_id=current_user.id,
        )
        flash("شكراً — وصلت ملاحظاتك لفريق المشروع.", "success")
    except (FeedbackError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal.project_detail", project_id=p.id))
