"""CRM — Leads blueprint.

Routes live at `/leads/*` as a native Marsoud module (not nested under
`/opsflow/`). All queries are scoped to `g.active_company.id`; sales reps
see only their own leads, sales managers / admins / owners see all.
"""
from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, abort
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db
from app.models import (
    Lead, LeadStatus, LeadStatusEvent,
    User, Customer, Employee,
)
from app.models.user import user_companies
from app.services.crm import (
    change_lead_status, convert_lead_to_project, CRMError,
)
from app.services.permissions import (
    require_permission, get_user_role,
)

bp = Blueprint("leads", __name__)


# ─── Role helpers ────────────────────────────────────────────────────────
FULL_VISIBILITY = {"owner", "admin", "sales_manager"}


def _can_view_all_leads():
    role = get_user_role(current_user.id, g.active_company.id)
    return role in FULL_VISIBILITY


def _company_user_ids():
    """All user ids that belong to the active company — used to scope dropdowns."""
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    return {r.user_id for r in rows}


def _sales_reps():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == cid) &
            (user_companies.c.role.in_(
                ["sales_rep", "sales_manager", "admin", "owner"],
            ))
        )
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _project_managers():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == cid) &
            (user_companies.c.role.in_(
                ["project_manager", "admin", "owner"],
            ))
        )
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _lead_or_403(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead or lead.company_id != g.active_company.id:
        abort(404)
    if not _can_view_all_leads() and lead.assigned_to_id != current_user.id:
        abort(403)
    return lead


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_datetime_local(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return None


# ─── List + filters ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("leads.view")
def index():
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    rep_filter = (request.args.get("rep") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    query = Lead.query.filter_by(company_id=cid)
    if not _can_view_all_leads():
        query = query.filter(Lead.assigned_to_id == current_user.id)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.client_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.service_needed.ilike(like),
        ))
    if status_filter:
        try:
            query = query.filter(Lead.status == LeadStatus[status_filter])
        except KeyError:
            pass
    if rep_filter:
        try:
            query = query.filter(Lead.assigned_to_id == int(rep_filter))
        except (TypeError, ValueError):
            pass
    df = _parse_date(date_from)
    if df:
        query = query.filter(Lead.created_at >= datetime.combine(df, datetime.min.time()))
    dt = _parse_date(date_to)
    if dt:
        query = query.filter(Lead.created_at < datetime.combine(dt + timedelta(days=1), datetime.min.time()))

    leads = query.order_by(Lead.created_at.desc()).all()

    # Status counts within current visibility scope
    base = Lead.query.filter_by(company_id=cid)
    if not _can_view_all_leads():
        base = base.filter(Lead.assigned_to_id == current_user.id)
    status_counts = {s: base.filter(Lead.status == s).count() for s in LeadStatus}

    return render_template(
        "leads/index.html",
        leads=leads, statuses=LeadStatus, status_counts=status_counts,
        reps=_sales_reps() if _can_view_all_leads() else [],
        q=q, status_filter=status_filter, rep_filter=rep_filter,
        date_from=date_from, date_to=date_to,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("leads.manage")
def new():
    if request.method == "POST":
        try:
            assigned_id = int(request.form.get("assigned_to_id"))
            if assigned_id not in _company_user_ids():
                raise CRMError("المسؤول يجب أن يكون من فريق هذه الشركة")
            lead = Lead(
                company_id=g.active_company.id,
                client_name=request.form.get("client_name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=(request.form.get("email") or "").strip().lower() or None,
                service_needed=request.form.get("service_needed", "").strip(),
                source=(request.form.get("source") or "").strip() or None,
                assigned_to_id=assigned_id,
                next_meeting=_parse_datetime_local(request.form.get("next_meeting")),
                meeting_notes=(request.form.get("meeting_notes") or "").strip() or None,
                status=LeadStatus.NEW_LEAD,
            )
            if not lead.client_name or not lead.phone or not lead.service_needed:
                raise CRMError("الاسم والهاتف والخدمة حقول مطلوبة")
            db.session.add(lead)
            db.session.flush()
            db.session.add(LeadStatusEvent(
                lead_id=lead.id, from_status=None,
                to_status=LeadStatus.NEW_LEAD,
                changed_by_id=current_user.id,
                note="إنشاء العميل المحتمل",
            ))
            db.session.commit()
            flash(f"تم إنشاء عميل محتمل: {lead.client_name}", "success")
            return redirect(url_for("leads.detail", lead_id=lead.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/form.html", lead=None,
                           reps=_sales_reps())


@bp.route("/<int:lead_id>")
@login_required
@require_permission("leads.view")
def detail(lead_id):
    lead = _lead_or_403(lead_id)
    can_manage_files = _can_view_all_leads() or lead.assigned_to_id == current_user.id
    return render_template("leads/detail.html",
                           lead=lead, statuses=LeadStatus,
                           can_manage_files=can_manage_files)


@bp.route("/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("leads.manage")
def edit(lead_id):
    lead = _lead_or_403(lead_id)
    if request.method == "POST":
        try:
            assigned_id = int(request.form.get("assigned_to_id"))
            if assigned_id not in _company_user_ids():
                raise CRMError("المسؤول يجب أن يكون من فريق هذه الشركة")
            lead.client_name = request.form.get("client_name", lead.client_name).strip()
            lead.phone = request.form.get("phone", lead.phone).strip()
            lead.email = (request.form.get("email") or "").strip().lower() or None
            lead.service_needed = request.form.get("service_needed", lead.service_needed).strip()
            lead.source = (request.form.get("source") or "").strip() or None
            lead.assigned_to_id = assigned_id
            lead.next_meeting = _parse_datetime_local(request.form.get("next_meeting"))
            lead.meeting_notes = (request.form.get("meeting_notes") or "").strip() or None
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("leads.detail", lead_id=lead.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/form.html", lead=lead, reps=_sales_reps())


@bp.route("/<int:lead_id>/status", methods=["POST"])
@login_required
@require_permission("leads.manage")
def status(lead_id):
    lead = _lead_or_403(lead_id)
    try:
        change_lead_status(
            lead, request.form.get("new_status"),
            changed_by_id=current_user.id,
            note=request.form.get("note"),
            lost_reason=request.form.get("lost_reason"),
        )
        flash(f"تم تغيير الحالة إلى: {lead.status.label_ar}", "success")
    except CRMError as e:
        flash(str(e), "error")
    return redirect(url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:lead_id>/convert", methods=["GET", "POST"])
@login_required
@require_permission("leads.convert")
def convert(lead_id):
    lead = _lead_or_403(lead_id)
    if lead.status != LeadStatus.WON:
        flash("لا يمكن التحويل قبل الوصول لحالة (ربحناها)", "error")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    if lead.is_converted:
        flash("تم تحويل هذا العميل المحتمل مسبقاً", "info")
        return redirect(url_for("leads.detail", lead_id=lead.id))

    pms = _project_managers()
    if request.method == "POST":
        try:
            project = convert_lead_to_project(
                lead,
                project_name=request.form.get("project_name", ""),
                project_type=request.form.get("project_type", ""),
                manager_id=int(request.form.get("manager_id")),
                start_date=_parse_date(request.form.get("start_date")),
                end_date=_parse_date(request.form.get("end_date")),
                created_by_id=current_user.id,
            )
            flash(f"تم تحويل العميل المحتمل إلى مشروع: {project.name}", "success")
            return redirect(url_for("projects.detail", project_id=project.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/convert.html",
                           lead=lead, pms=pms,
                           default_name=f"مشروع: {lead.service_needed} — {lead.client_name}",
                           default_type=lead.service_needed)
