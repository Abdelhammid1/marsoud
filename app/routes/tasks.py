"""Tasks blueprint — Kanban + task CRUD, native Marsoud module.

Visibility:
  - owner / admin / project_manager: every task in the company.
  - team_member: tasks assigned to them.
Move-status is allowed for the assignee + everyone with full visibility.
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, abort
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db
from app.models import (
    Task, TaskStatus, TaskPriority, KANBAN_ORDER,
    Project, ProjectStatus, Milestone, User,
)
from app.models.user import user_companies
from app.services.crm import set_task_status, CRMError
from app.services.permissions import (
    require_permission, get_user_role,
)

bp = Blueprint("tasks", __name__)


FULL_VISIBILITY = {"owner", "admin", "project_manager"}


def _role():
    return get_user_role(current_user.id, g.active_company.id)


def _visible_tasks_query():
    cid = g.active_company.id
    q = Task.query.filter_by(company_id=cid)
    role = _role()
    if role in FULL_VISIBILITY:
        if role == "project_manager":
            # PM sees tasks in projects they manage OR they're assigned to
            pm_pids = db.session.query(Project.id).filter(
                Project.company_id == cid, Project.manager_id == current_user.id,
            )
            q = q.filter(or_(
                Task.project_id.in_(pm_pids),
                Task.assigned_to_id == current_user.id,
            ))
        # owner / admin: all tasks → no filter
        return q
    if role == "team_member":
        return q.filter(Task.assigned_to_id == current_user.id)
    return q.filter(False)


def _company_users():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _company_projects():
    cid = g.active_company.id
    return Project.query.filter_by(company_id=cid).order_by(Project.name).all()


def _task_or_403(task_id):
    t = db.session.get(Task, task_id)
    if not t or t.company_id != g.active_company.id:
        abort(404)
    role = _role()
    if role in FULL_VISIBILITY:
        if role == "project_manager" and t.assigned_to_id != current_user.id:
            # PM restricted to tasks they manage — only if there's a project.
            # Standalone tasks (no project) fall back to assignee-only access.
            if not t.project or t.project.manager_id != current_user.id:
                abort(403)
        return t
    if role == "team_member" and t.assigned_to_id == current_user.id:
        return t
    abort(403)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ─── Kanban + filters ────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("tasks.view")
def index():
    project_filter = request.args.get("project_id")
    priority_filter = request.args.get("priority")
    assignee_filter = request.args.get("assignee")

    q = _visible_tasks_query()
    if project_filter:
        try:
            q = q.filter(Task.project_id == int(project_filter))
        except (TypeError, ValueError):
            pass
    if priority_filter:
        try:
            q = q.filter(Task.priority == TaskPriority[priority_filter])
        except KeyError:
            pass
    if assignee_filter:
        try:
            q = q.filter(Task.assigned_to_id == int(assignee_filter))
        except (TypeError, ValueError):
            pass

    tasks = q.order_by(Task.deadline.asc().nullslast(), Task.created_at.desc()).all()
    columns = {s: [] for s in KANBAN_ORDER}
    for t in tasks:
        columns.setdefault(t.status, []).append(t)

    return render_template("tasks/index.html",
                           columns=columns, kanban=KANBAN_ORDER,
                           projects=_company_projects(),
                           users=_company_users(),
                           priorities=TaskPriority,
                           project_filter=project_filter,
                           priority_filter=priority_filter,
                           assignee_filter=assignee_filter)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("tasks.manage")
def new():
    cid = g.active_company.id
    projects = _company_projects()
    users = _company_users()
    if request.method == "POST":
        try:
            # MARSOUD-27 — project is now optional (standalone task)
            pid_raw = request.form.get("project_id") or None
            pid = int(pid_raw) if pid_raw else None
            project = None
            if pid:
                project = db.session.get(Project, pid)
                if not project or project.company_id != cid:
                    raise CRMError("المشروع غير موجود")
            assignee_id = int(request.form.get("assigned_to_id"))
            milestone_raw = request.form.get("milestone_id") or None
            milestone_id = int(milestone_raw) if milestone_raw else None
            if milestone_id:
                if not project:
                    raise CRMError("لا يمكن ربط مرحلة بدون مشروع")
                m = db.session.get(Milestone, milestone_id)
                if not m or m.project_id != pid:
                    raise CRMError("المرحلة لا تنتمي لهذا المشروع")
            priority_str = request.form.get("priority", "MEDIUM")
            t = Task(
                company_id=cid,
                title=(request.form.get("title") or "").strip(),
                description=(request.form.get("description") or "").strip() or None,
                project_id=pid,
                milestone_id=milestone_id,
                assigned_to_id=assignee_id,
                priority=TaskPriority[priority_str],
                status=TaskStatus.TODO,
                deadline=_parse_date(request.form.get("deadline")),
                notes=(request.form.get("notes") or "").strip() or None,
            )
            if not t.title:
                raise CRMError("عنوان المهمة مطلوب")
            db.session.add(t)
            db.session.commit()
            if project:
                project.recompute_progress()
                db.session.commit()
            # FR-22: notification on new task assignment
            try:
                from app.services.opsflow_extras import notify
                from app.models import NotificationKind
                if t.assigned_to_id and t.assigned_to_id != current_user.id:
                    notify(
                        t.assigned_to_id, company_id=cid,
                        kind=NotificationKind.TASK_ASSIGNED,
                        title=f"📌 مهمة جديدة: {t.title}",
                        body=(t.description or "")[:200],
                        link_url=f"/tasks/{t.id}",
                    )
            except Exception:
                from flask import current_app
                current_app.logger.exception("task assign notify failed")
            flash(f"تم إنشاء المهمة: {t.title}", "success")
            return redirect(url_for("tasks.detail", task_id=t.id))
        except (CRMError, ValueError, TypeError, KeyError) as e:
            flash(str(e), "error")
    # Pre-select project from query string
    selected_project = request.args.get("project_id")
    return render_template("tasks/form.html",
                           task=None, projects=projects, users=users,
                           priorities=TaskPriority, milestones=[],
                           selected_project=selected_project)


@bp.route("/<int:task_id>")
@login_required
@require_permission("tasks.view")
def detail(task_id):
    t = _task_or_403(task_id)
    from app.services.opsflow_extras import documents_for
    docs = documents_for("TASK", t.id)
    return render_template("tasks/detail.html",
                           task=t, statuses=TaskStatus, docs=docs)


@bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("tasks.manage")
def edit(task_id):
    t = _task_or_403(task_id)
    projects = _company_projects()
    users = _company_users()
    if request.method == "POST":
        try:
            t.title = (request.form.get("title") or t.title).strip()
            t.description = (request.form.get("description") or "").strip() or None
            t.assigned_to_id = int(request.form.get("assigned_to_id"))
            milestone_raw = request.form.get("milestone_id") or None
            t.milestone_id = int(milestone_raw) if milestone_raw else None
            priority_str = request.form.get("priority", t.priority.value)
            t.priority = TaskPriority[priority_str]
            t.deadline = _parse_date(request.form.get("deadline"))
            t.notes = (request.form.get("notes") or "").strip() or None
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("tasks.detail", task_id=t.id))
        except (ValueError, TypeError, KeyError) as e:
            flash(str(e), "error")
    return render_template("tasks/form.html",
                           task=t, projects=projects, users=users,
                           priorities=TaskPriority,
                           milestones=t.project.milestones if t.project else [])


@bp.route("/<int:task_id>/status", methods=["POST"])
@login_required
@require_permission("tasks.manage")
def status(task_id):
    t = _task_or_403(task_id)
    try:
        set_task_status(t, request.form.get("new_status"), by_user_id=current_user.id)
        flash(f"تم تحديث الحالة إلى: {t.status.label_ar}", "success")
    except CRMError as e:
        flash(str(e), "error")
    # Where to bounce back to? If from kanban → back to kanban.
    if request.form.get("return_to") == "kanban":
        return redirect(url_for("tasks.index"))
    return redirect(url_for("tasks.detail", task_id=t.id))
