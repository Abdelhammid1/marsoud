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
    """MARSOUD-PM-DROPDOWN-FIX (Abdelhamid 2026-07-11) — the "Project
    Manager" picker used to filter by role name only, which meant
    that granting a custom role all the same permissions as
    project_manager still didn't unlock the dropdown. Rofida's ticket:
    "I gave her the same permissions as PM but she still doesn't show —
    she only shows when I make her a Project Manager role explicitly."

    Fix: enumerate every company member and keep whoever actually
    has `projects.manage`. This honours DB-backed permissions on
    custom cloned roles, not just the built-in role name."""
    cid = g.active_company.id
    company = g.active_company
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    out = []
    for r in rows:
        u = db.session.get(User, r.user_id)
        if not u:
            continue
        if has_permission("projects.manage", user=u, company=company):
            out.append(u)
    return out


def _user_can_see_project(project):
    """MARSOUD-PERM-FIX (PM scope) — direct URL access (e.g. /projects/<id>)
    must enforce the same scope as the list page. Order:
      1. Soft-deleted projects → only super-admin (handled upstream); all
         other readers get 404 even if they used to be allowed.
      2. projects.view_all → see anything.
      3. Manager of this project → see it.
      4. Member of this project → see it.
      5. Sales user whose lead converted into this project → see it.
    Anything else → 403 from the caller."""
    if project.deleted_at is not None and not getattr(
        current_user, "is_superadmin", False
    ):
        return False
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
    # MARSOUD-L: soft-deleted projects always hidden from the list.
    # MARSOUD-PROJECT-ARCHIVE (2026-08-10) — hide archived
    # projects from the default list. They land on
    # /projects/archive/ instead. The `scope=archive` query
    # arg opts back in so users can find them from a
    # bookmarked filter URL.
    query = Project.query.filter_by(company_id=cid).filter(
        Project.deleted_at.is_(None))
    if request.args.get("scope") != "archive":
        query = query.filter(Project.archived_at.is_(None))
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
            # PER-CO-NUMBERING (Abdelhamid 2026-07-04) — assign a
            # per-company display number ("PRJ-0001").
            from app.services.numbering import next_number
            _proj_number = next_number(cid, "PROJECT")
            p = Project(
                company_id=cid,
                number=_proj_number,
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


@bp.route("/<int:project_id>/milestones/<int:m_id>/edit", methods=["POST"])
@login_required
@require_permission("projects.manage")
def milestone_edit(project_id, m_id):
    """Edit an existing milestone's name and/or target date. Tasks linked
    to this milestone stay linked (milestone_id doesn't change)."""
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    m = db.session.get(Milestone, m_id)
    if not m or m.project_id != p.id:
        flash("المرحلة غير موجودة", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        flash("اسم المرحلة مطلوب", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    m.name = new_name
    m.target_date = _parse_date(request.form.get("target_date"))
    db.session.commit()
    flash(f"تم حفظ التعديلات على: {m.name}", "success")
    return redirect(url_for("projects.detail", project_id=p.id))


@bp.route("/<int:project_id>/milestones/<int:m_id>/delete", methods=["POST"])
@login_required
@require_permission("projects.manage")
def milestone_delete(project_id, m_id):
    """Delete a milestone. Any tasks linked to it are UN-linked
    (milestone_id → NULL), NOT deleted — the tasks live on."""
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    m = db.session.get(Milestone, m_id)
    if not m or m.project_id != p.id:
        flash("المرحلة غير موجودة", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    # Un-link tasks (Task has milestone_id nullable)
    from app.models import Task
    Task.query.filter_by(milestone_id=m.id).update({"milestone_id": None})
    name = m.name
    db.session.delete(m)
    db.session.commit()
    flash(f"تم حذف المرحلة: {name} — المهام المرتبطة بها بقيت من غير مرحلة.",
          "success")
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


# ─── MARSOUD-L: owner-only project edit + soft-delete ──────────────────
def _owner_only():
    """Hard gate: even project managers can't bypass the role check."""
    return _role() == "owner" or has_permission("projects.view_all") and _role() == "admin"


@bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("projects.manage")
def edit(project_id):
    """Owner-only project metadata edit."""
    p = _project_or_403(project_id)
    if _role() not in ("owner", "admin"):
        abort(403)
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            if not name:
                raise CRMError("اسم المشروع مطلوب")
            p.name = name
            p.type = (request.form.get("type") or "").strip() or p.type
            new_manager = request.form.get("manager_id")
            if new_manager:
                p.manager_id = int(new_manager)
            sd = _parse_date(request.form.get("start_date"))
            ed = _parse_date(request.form.get("end_date"))
            if sd: p.start_date = sd
            if ed: p.end_date = ed
            p.notes = (request.form.get("notes") or "").strip() or None
            db.session.commit()
            flash(f"تم حفظ تعديلات: {p.name}", "success")
            return redirect(url_for("projects.detail", project_id=p.id))
        except (CRMError, ValueError, TypeError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("projects/edit.html",
                            project=p,
                            managers=_project_managers())


@bp.route("/<int:project_id>/delete", methods=["POST"])
@login_required
def delete(project_id):
    """Owner-only soft delete with reason."""
    from app.services.lifecycle import soft_delete_project
    p = _project_or_403(project_id)
    if _role() not in ("owner", "admin"):
        flash("فقط المالك يقدر يحذف المشروع", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("لازم تكتب سبب الحذف", "error")
        return redirect(url_for("projects.detail", project_id=p.id))
    soft_delete_project(p, actor_id=current_user.id, reason=reason)
    flash(f"تم حذف المشروع '{p.name}' (قابل للاستعادة).", "success")
    return redirect(url_for("projects.index"))


# ─── MARSOUD-PROJECT-ARCHIVE (2026-08-10) — archive lifecycle ─────
# Users' mental model is "when a project is done, put it away"
# — an action task-archive already ships for tasks. These three
# routes mirror app/routes/tasks.py:984-1072's shape so the two
# archives feel consistent from the sidebar to the button labels.
@bp.route("/<int:project_id>/archive", methods=["POST"])
@login_required
@require_permission("projects.archive")
def archive(project_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        # Same scope as edit: owner/admin globally, PM for their
        # own projects. `has_permission("projects.archive")`
        # covers the role catalogue but a PM shouldn't archive
        # someone else's project.
        abort(403)
    from app.services.project_archive import archive_project
    changed = archive_project(p, actor_id=current_user.id)
    if changed:
        flash(f"✅ تم أرشفة المشروع: {p.name}", "success")
    else:
        flash("المشروع مؤرشف بالفعل", "info")
    return redirect(url_for("projects.index"))


@bp.route("/<int:project_id>/unarchive", methods=["POST"])
@login_required
@require_permission("projects.archive")
def unarchive(project_id):
    p = _project_or_403(project_id)
    if not _user_can_edit_project(p):
        abort(403)
    from app.services.project_archive import unarchive_project
    changed = unarchive_project(p, actor_id=current_user.id)
    if changed:
        flash(f"✅ تم استعادة المشروع: {p.name}", "success")
    else:
        flash("المشروع غير مؤرشف", "info")
    return redirect(url_for("projects.detail", project_id=p.id))


@bp.route("/archive/")
@login_required
@require_permission("projects.archive")
def archive_index():
    """List every archived project the current user can see.
    Owners/admins see the whole company's archive; PMs see
    only their own archived projects."""
    from app.services.project_archive import archived_projects_for
    role = _role()
    full = (role in FULL_VISIBILITY) or has_permission(
        "projects.view_all")
    rows = archived_projects_for(
        current_user, g.active_company.id, full_view=full)
    return render_template(
        "projects/archive.html",
        projects=rows, can_full_view=full,
    )
