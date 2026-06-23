"""Projects blueprint — native Marsoud module.

Role visibility:
  - owner / admin: every project in the company.
  - project_manager: projects they manage.
  - team_member: projects they're a member of.
  - sales_manager / sales_rep: projects converted from leads they own.
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, abort
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db
from app.models import (
    Project, ProjectStatus, PROJECT_TRANSITIONS,
    ProjectMember, Milestone, ProjectStatusEvent,
    Task, TaskStatus, KANBAN_ORDER,
    Lead, Customer, User,
)
from app.models.user import user_companies
from app.services.crm import change_project_status, CRMError
from app.services.permissions import (
    require_permission, get_user_role, has_permission,
)

bp = Blueprint("projects", __name__)


FULL_VISIBILITY = {"owner", "admin"}


def _role():
    return get_user_role(current_user.id, g.active_company.id)


def _can_view_all_projects():
    """MARSOUD-PERM-FIX (PM scope) — permission-based. Custom roles that
    grant projects.view_all see every project; legacy owner/admin still
    pass via the role-name fallback for the first-boot window."""
    if has_permission("projects.view_all"):
        return True
    return _role() in FULL_VISIBILITY


def _company_users():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _project_managers():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == cid) &
            (user_companies.c.role.in_(["project_manager", "admin", "owner"]))
        )
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _user_can_see_project(project):
    """MARSOUD-PERM-FIX (PM scope) — direct URL access (e.g. /projects/<id>)
    must enforce the same scope as the list page. Order:
      1. projects.view_all → see anything.
      2. Manager of this project → see it.
      3. Member of this project → see it.
      4. Sales user whose lead converted into this project → see it.
    Anything else → 403 from the caller."""
    if has_permission("projects.view_all"):
        return True
    if _role() in FULL_VISIBILITY:
        return True
    if project.manager_id == current_user.id:
        return True
    if any(m.user_id == current_user.id for m in project.members):
        return True
    role = _role()
    if role in ("sales_manager", "sales_rep"):
        return bool(project.lead and project.lead.assigned_to_id == current_user.id)
    return False


def _user_can_edit_project(project):
    role = _role()
    if role in FULL_VISIBILITY:
        return True
    return role == "project_manager" and project.manager_id == current_user.id


def _project_or_403(project_id):
    p = db.session.get(Project, project_id)
    if not p or p.company_id != g.active_company.id:
        abort(404)
    if not _user_can_see_project(p):
        abort(403)
    return p


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ─── List + filters ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("projects.view")
def index():
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    manager_filter = (request.args.get("manager") or "").strip()

    # MARSOUD-PERM-FIX (PM scope) — switch from hardcoded role checks to a
    # permission-driven filter. `projects.view_all` is the unambiguous
    # "see every project" gate (owner / admin / ceo by default). Everyone
    # else sees the union of: projects they manage, projects they're a
    # member of, and (for sales) projects converted from a lead they own.
    query = Project.query.filter_by(company_id=cid)
    role = _role()
    if not has_permission("projects.view_all"):
        member_pids = db.session.query(ProjectMember.project_id).filter(
            ProjectMember.user_id == current_user.id,
        )
        scope_clauses = [
            Project.manager_id == current_user.id,
            Project.id.in_(member_pids),
        ]
        if role in ("sales_manager", "sales_rep"):
            lead_ids = db.session.query(Lead.id).filter(
                Lead.company_id == cid, Lead.assigned_to_id == current_user.id,
            )
            scope_clauses.append(Project.lead_id.in_(lead_ids))
        query = query.filter(or_(*scope_clauses))

    if q:
        like = f"%{q}%"
        query = query.filter(or_(Project.name.ilike(like), Project.type.ilike(like)))
    if status_filter:
        try:
            query = query.filter(Project.status == ProjectStatus[status_filter])
        except KeyError:
            pass
    if manager_filter:
        try:
            query = query.filter(Project.manager_id == int(manager_filter))
        except (TypeError, ValueError):
            pass

    projects = query.order_by(Project.created_at.desc()).all()
    return render_template(
        "projects/index.html",
        projects=projects, statuses=ProjectStatus,
        pms=_project_managers() if _can_view_all_projects() else [],
        q=q, status_filter=status_filter, manager_filter=manager_filter,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("projects.create")
def new():
    cid = g.active_company.id
    customers = Customer.query.filter_by(company_id=cid).order_by(Customer.name).all()
    pms = _project_managers()
    if request.method == "POST":
        try:
            customer_id = int(request.form.get("customer_id"))
            cust = db.session.get(Customer, customer_id)
            if not cust or cust.company_id != cid:
                raise CRMError("العميل غير موجود في هذه الشركة")
            manager_id = int(request.form.get("manager_id"))
            sd = _parse_date(request.form.get("start_date"))
            ed = _parse_date(request.form.get("end_date"))
            if not sd or not ed:
                raise CRMError("تواريخ البداية والنهاية مطلوبة")
            if ed <= sd:
                raise CRMError("تاريخ النهاية يجب أن يكون بعد البداية")
            p = Project(
                company_id=cid,
                name=request.form.get("name", "").strip(),
                customer_id=customer_id,
                type=request.form.get("type", "").strip(),
                manager_id=manager_id,
                start_date=sd, end_date=ed,
                notes=(request.form.get("notes") or "").strip() or None,
                status=ProjectStatus.PLANNING,
            )
            if not p.name or not p.type:
                raise CRMError("اسم المشروع والنوع مطلوبان")
            db.session.add(p)
            db.session.flush()
            db.session.add(ProjectStatusEvent(
                project_id=p.id, from_status=None,
                to_status=ProjectStatus.PLANNING,
                changed_by_id=current_user.id, note="إنشاء المشروع",
            ))
            db.session.commit()
            flash(f"تم إنشاء المشروع: {p.name}", "success")
            return redirect(url_for("projects.detail", project_id=p.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("projects/form.html",
                           project=None, customers=customers, pms=pms)


@bp.route("/<int:project_id>")
@login_required
@require_permission("projects.view")
def detail(project_id):
    p = _project_or_403(project_id)
    p.recompute_progress()
    db.session.commit()
    next_statuses = PROJECT_TRANSITIONS.get(p.status, [])
    member_ids = {m.user_id for m in p.members} | {p.manager_id}
    candidates = [u for u in _company_users() if u and u.id not in member_ids]
    # Tasks grouped by status for the inline Kanban summary
    tasks = Task.query.filter_by(project_id=p.id).order_by(Task.created_at.desc()).all()
    by_status = {s: [] for s in KANBAN_ORDER}
    for t in tasks:
        by_status.setdefault(t.status, []).append(t)
    can_edit = _user_can_edit_project(p)
    from app.services.opsflow_extras import documents_for
    docs = documents_for("PROJECT", p.id)
    return render_template("projects/detail.html",
                           project=p, next_statuses=next_statuses,
                           candidates=candidates, by_status=by_status,
                           statuses=ProjectStatus, can_edit=can_edit,
                           docs=docs)


@bp.route("/<int:project_id>/status", methods=["POST"])
@login_required
@require_permission("projects.manage")
def status(project_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    try:
        change_project_status(
            p, request.form.get("new_status"),
            changed_by_id=current_user.id,
            note=request.form.get("note"),
        )
        flash(f"تم تغيير الحالة إلى: {p.status.label_ar}", "success")
    except CRMError as e:
        flash(str(e), "error")
    return redirect(url_for("projects.detail", project_id=p.id))


# ─── Milestones ──────────────────────────────────────────────────────────
@bp.route("/<int:project_id>/milestones/new", methods=["POST"])
@login_required
@require_permission("projects.manage")
def milestone_new(project_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم المرحلة مطلوب", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    next_order = (db.session.query(db.func.coalesce(db.func.max(Milestone.order), 0))
                  .filter_by(project_id=p.id).scalar() or 0) + 1
    m = Milestone(
        project_id=p.id, name=name,
        target_date=_parse_date(request.form.get("target_date")),
        order=next_order,
    )
    db.session.add(m)
    db.session.commit()
    flash(f"تم إنشاء المرحلة: {name}", "success")
    return redirect(url_for("projects.detail", project_id=p.id))


@bp.route("/<int:project_id>/milestones/<int:m_id>/toggle", methods=["POST"])
@login_required
@require_permission("projects.manage")
def milestone_toggle(project_id, m_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    m = db.session.get(Milestone, m_id)
    if not m or m.project_id != p.id:
        flash("المرحلة غير موجودة", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    m.completed_at = None if m.completed_at else datetime.utcnow()
    db.session.commit()
    return redirect(url_for("projects.detail", project_id=p.id))


# ─── Members ─────────────────────────────────────────────────────────────
@bp.route("/<int:project_id>/members/add", methods=["POST"])
@login_required
@require_permission("projects.manage")
def member_add(project_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    try:
        uid = int(request.form.get("user_id"))
    except (TypeError, ValueError):
        flash("اختر عضواً للإضافة", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    user = db.session.get(User, uid)
    if not user or user.id not in {u.id for u in _company_users() if u}:
        flash("هذا المستخدم ليس عضواً في فريق الشركة", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    exists = ProjectMember.query.filter_by(project_id=p.id, user_id=uid).first()
    if exists:
        flash("هذا العضو موجود بالفعل في الفريق", "warning")
    else:
        db.session.add(ProjectMember(project_id=p.id, user_id=uid))
        db.session.commit()
        flash(f"تم إضافة {user.full_name} لفريق المشروع", "success")
    return redirect(url_for("projects.detail", project_id=p.id))


@bp.route("/<int:project_id>/members/<int:member_id>/remove", methods=["POST"])
@login_required
@require_permission("projects.manage")
def member_remove(project_id, member_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    m = db.session.get(ProjectMember, member_id)
    if m and m.project_id == p.id:
        db.session.delete(m)
        db.session.commit()
        flash("تم إخراج العضو من الفريق", "success")
    return redirect(url_for("projects.detail", project_id=p.id))
