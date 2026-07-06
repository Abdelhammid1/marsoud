"""Tasks blueprint — Kanban + task CRUD + multi-assignee (MARSOUD-TASKS-02).

Visibility:
  - owner / admin: every task in the company.
  - project_manager: tasks in projects they manage OR they're assigned to
    (legacy primary OR member of task_assignees).
  - team_member: only tasks they're assigned to (primary or member).
Status changes are allowed for anyone with task visibility.
"""
from datetime import datetime, date
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
    jsonify,
)
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
    require_permission, get_user_role, has_permission,
)
from app.services.tasks_extras import (
    TaskError,
    visible_tasks_query, is_visible_to,
    set_assignees, assignee_ids_for,
    add_comment, apply_inline_edit, log_activity,
    team_stats, delete_task_fully,
)

bp = Blueprint("tasks", __name__)


# MARSOUD-PERM-FIX-01 — legacy role list kept as a fallback. Canonical
# check is the `tasks.view_all` permission (auto-attached to these roles
# on boot by roles_seed.seed_system_roles_for_company).
FULL_VISIBILITY = {"owner", "admin"}


def _role():
    return get_user_role(current_user.id, g.active_company.id)


def _has_full_task_visibility():
    """Permission-based, with role-name fallback for the first-boot window."""
    if has_permission("tasks.view_all"):
        return True
    return _role() in FULL_VISIBILITY


def _pm_project_ids():
    cid = g.active_company.id
    rows = db.session.query(Project.id).filter(
        Project.company_id == cid, Project.manager_id == current_user.id,
    ).all()
    return [r[0] for r in rows]


def _visible_tasks_query():
    cid = g.active_company.id
    full = _has_full_task_visibility()
    pm_pids = _pm_project_ids() if _role() == "project_manager" else None
    return visible_tasks_query(cid, current_user.id, full, pm_pids)


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
    full = _has_full_task_visibility()
    pm_pids = _pm_project_ids() if _role() == "project_manager" else None
    if not is_visible_to(t, current_user.id, full, pm_pids):
        abort(403)
    return t


def _can_edit_description(task):
    """MARSOUD — the task description belongs to whoever created it.
    Nobody else (not even admin) can change the wording. Other fields
    (status/priority/deadline/assignees) follow the normal tasks.manage
    permission as before."""
    return (task.created_by_id is not None
            and task.created_by_id == current_user.id)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_assignee_ids(form):
    """Read either multi-select assignee_ids or fall back to single field."""
    ids = form.getlist("assignee_ids") or []
    if not ids and form.get("assigned_to_id"):
        ids = [form.get("assigned_to_id")]
    out = []
    for raw in ids:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            pass
    return out


def _safe_next(default):
    """MARSOUD-TASK-CONTEXT — preserve the caller's viewport across a
    CRUD round-trip.

    Reads a `return_to` field from POST form → query args → referrer,
    in that order. Only returns it when it's a same-origin path
    (leading '/', no protocol, no CRLF injection). Otherwise falls
    back to `default`.

    Used by new()/edit()/delete()/status() so that a task created
    from /tasks/?scope=employees&user_id=15 lands the user back
    inside that view, not on the default 'mine' Kanban.
    """
    from urllib.parse import urlparse
    candidate = (request.form.get("return_to")
                 or request.args.get("return_to")
                 or "")
    candidate = candidate.strip()
    if not candidate:
        return default
    # Reject anything that isn't a local path — the leading `/` alone
    # isn't enough because `//attacker.com/x` is protocol-relative and
    # would let a phisher hijack the redirect.
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if "\r" in candidate or "\n" in candidate:
        return default
    # Bounce parsed check as a belt-and-braces guard: netloc must be
    # empty for a path-only URL.
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


# ─── Kanban + filters ────────────────────────────────────────────────────
# MARSOUD-TASK-SCOPE-01 — 4 tabs over the Kanban board, visible to
# owner/manager (anyone with tasks.view_all). Regular employees still
# see only "my tasks + tasks I created" with no tabs.
TASK_SCOPES = {"mine", "created", "employees", "all"}


def _employee_monthly_stats(company_id, user_id, months=6):
    """MARSOUD-TASK-ARCHIVE-01 — last N months of tasks-closed-per-month
    for a single employee, used by the drill-down view to surface
    performance trend over time. Counts INCLUDE archived rows (the
    ticket says 'كل ما يخص الموظف').
    """
    import json as _json
    from datetime import datetime as _dt
    from app.models import Task, TaskActivityLog, task_assignees as _ta
    # Step 1: get every task this user is involved with.
    rows = db.session.execute(
        _ta.select().where(_ta.c.user_id == user_id)
    ).fetchall()
    task_ids = {r.task_id for r in rows}
    # Also include legacy primary-assignee tasks
    legacy = db.session.query(Task.id).filter(
        Task.company_id == company_id,
        Task.assigned_to_id == user_id,
    ).all()
    task_ids.update(r[0] for r in legacy)
    if not task_ids:
        return {"labels": [], "closed": [], "user_total": 0}
    # Step 2: walk DONE-transition events from the activity log.
    close_events = db.session.query(TaskActivityLog).filter(
        TaskActivityLog.task_id.in_(task_ids),
        TaskActivityLog.action == "STATUS_CHANGED",
    ).all()
    # Build monthly buckets for the last `months` months (oldest → newest)
    today = _dt.utcnow()
    buckets = []
    for i in range(months - 1, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12; y -= 1
        buckets.append((y, m))
    closed_by_month = {(y, m): 0 for y, m in buckets}
    seen = set()
    for ev in close_events:
        if ev.task_id in seen:
            continue
        try:
            after = _json.loads(ev.after_json or "{}")
        except (TypeError, ValueError):
            continue
        if (after.get("status") or "").upper() != "DONE":
            continue
        ts = ev.created_at
        key = (ts.year, ts.month)
        if key in closed_by_month:
            closed_by_month[key] += 1
        seen.add(ev.task_id)
    ar_months = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                  "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    labels = [f"{ar_months[m-1]}" for y, m in buckets]
    return {
        "labels": labels,
        "closed": [closed_by_month[k] for k in buckets],
        "user_total": len(task_ids),
    }


def _employee_task_buckets(company_id):
    """Aggregate per-employee task counters for the "Employees" tab.
    Returns one dict per active company member sorted by total task
    count desc. Cheap enough for v1 (small companies) — single Task
    table scan + a few in-Python groupings."""
    from datetime import date as _date
    from app.models import User, Task, TaskStatus, task_assignees
    members = db.session.execute(
        user_companies.select().where(
            user_companies.c.company_id == company_id,
        )
    ).fetchall()
    user_ids = [m.user_id for m in members]
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}
    rows = db.session.execute(
        task_assignees.select().where(
            task_assignees.c.user_id.in_(user_ids),
        )
    ).fetchall()
    task_id_to_uids = {}
    for r in rows:
        task_id_to_uids.setdefault(r.task_id, set()).add(r.user_id)
    # Exclude archived rows so the cards reflect what's still on the board.
    tasks = Task.query.filter_by(company_id=company_id).filter(
        Task.archived_at.is_(None),
    ).all()
    by_user = {uid: {"user": users[uid], "total": 0, "done": 0,
                     "in_progress": 0, "overdue": 0}
                for uid in user_ids if uid in users}
    today = _date.today()
    for t in tasks:
        uids = task_id_to_uids.get(t.id, set())
        if t.assigned_to_id:
            uids = uids | {t.assigned_to_id}
        for uid in uids:
            if uid not in by_user:
                continue
            b = by_user[uid]
            b["total"] += 1
            if t.status == TaskStatus.DONE:
                b["done"] += 1
            elif t.status == TaskStatus.IN_PROGRESS:
                b["in_progress"] += 1
            if t.deadline and t.deadline < today and t.status != TaskStatus.DONE:
                b["overdue"] += 1
    out = []
    for b in by_user.values():
        b["progress_pct"] = (round(b["done"] / b["total"] * 100)
                              if b["total"] else 0)
        out.append(b)
    out.sort(key=lambda r: -r["total"])
    return out


@bp.route("/")
@login_required
@require_permission("tasks.view")
def index():
    project_filter = request.args.get("project_id")
    priority_filter = request.args.get("priority")
    assignee_filter = request.args.get("assignee")

    # MARSOUD-TASK-SCOPE-01 — tabs for owner/manager.
    can_see_all = _has_full_task_visibility()
    scope = (request.args.get("scope") or "mine").lower()
    emp_user_id = request.args.get("user_id", type=int)
    # Defence-in-depth: a regular employee posting ?scope=all gets ignored
    # — they always see their own + created. The scope query parameter is
    # only honoured when the user actually has tasks.view_all.
    if not can_see_all:
        scope = "mine_or_created"   # synthetic — see assignees ∪ creator

    cid = g.active_company.id
    if not can_see_all:
        # Employees: their tasks + tasks they created. Tabs hidden.
        from app.models import task_assignees as _ta
        sub = db.session.query(_ta.c.task_id).filter(
            _ta.c.user_id == current_user.id,
        )
        q = Task.query.filter(Task.company_id == cid).filter(or_(
            Task.assigned_to_id == current_user.id,
            Task.id.in_(sub),
            Task.created_by_id == current_user.id,
        ))
    else:
        if scope == "created":
            q = Task.query.filter_by(
                company_id=cid, created_by_id=current_user.id,
            )
        elif scope == "all":
            q = Task.query.filter_by(company_id=cid)
        elif scope == "employees":
            from app.models import task_assignees as _ta
            if emp_user_id:
                # Drill-down: tasks where the picked employee is an assignee.
                sub = db.session.query(_ta.c.task_id).filter(
                    _ta.c.user_id == emp_user_id,
                )
                q = Task.query.filter(Task.company_id == cid).filter(or_(
                    Task.assigned_to_id == emp_user_id,
                    Task.id.in_(sub),
                ))
            else:
                # Cards landing — placeholder query (no rows will render
                # because employee_cards branch in the template fires).
                q = Task.query.filter(Task.id == 0)
        else:
            # Default: "mine" — tasks assigned to current_user.
            scope = "mine"
            from app.models import task_assignees as _ta
            sub = db.session.query(_ta.c.task_id).filter(
                _ta.c.user_id == current_user.id,
            )
            q = Task.query.filter(Task.company_id == cid).filter(or_(
                Task.assigned_to_id == current_user.id,
                Task.id.in_(sub),
            ))

    # MARSOUD-TASK-ARCHIVE-01 — every Kanban view hides archived tasks.
    q = q.filter(Task.archived_at.is_(None))

    # Employees-tab landing (no specific user yet) — render the cards page.
    employee_cards = None
    if can_see_all and scope == "employees" and not emp_user_id:
        employee_cards = _employee_task_buckets(cid)

    # Standard filters apply on top of the scope filter.
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
            from app.models import task_assignees
            aid = int(assignee_filter)
            sub = db.session.query(task_assignees.c.task_id).filter(
                task_assignees.c.user_id == aid,
            )
            q = q.filter(or_(Task.assigned_to_id == aid, Task.id.in_(sub)))
        except (TypeError, ValueError):
            pass

    if employee_cards is None:
        tasks = q.order_by(Task.deadline.asc().nullslast(),
                            Task.created_at.desc()).all()
    else:
        tasks = []
    columns = {s: [] for s in KANBAN_ORDER}
    for t in tasks:
        columns.setdefault(t.status, []).append(t)

    # If drill-down, surface the employee for the back-link banner +
    # build the monthly performance stats for the chart at the top.
    drill_user = None
    drill_monthly = None
    drill_archived_count = 0
    if can_see_all and scope == "employees" and emp_user_id:
        drill_user = db.session.get(User, emp_user_id)
        if drill_user:
            drill_monthly = _employee_monthly_stats(cid, emp_user_id)
            # Count archived tasks too so the drill page surfaces them.
            from app.models import task_assignees as _ta
            sub = db.session.query(_ta.c.task_id).filter(
                _ta.c.user_id == emp_user_id,
            )
            drill_archived_count = Task.query.filter(
                Task.company_id == cid,
                Task.archived_at.isnot(None),
            ).filter(or_(
                Task.assigned_to_id == emp_user_id,
                Task.id.in_(sub),
            )).count()

    return render_template(
        "tasks/index.html",
        columns=columns, kanban=KANBAN_ORDER,
        projects=_company_projects(),
        users=_company_users(),
        priorities=TaskPriority,
        project_filter=project_filter,
        priority_filter=priority_filter,
        assignee_filter=assignee_filter,
        # MARSOUD-TASK-SCOPE-01 context
        can_see_all=can_see_all,
        scope=scope,
        employee_cards=employee_cards,
        drill_user=drill_user,
        drill_monthly=drill_monthly,
        drill_archived_count=drill_archived_count,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("tasks.manage")
def new():
    cid = g.active_company.id
    projects = _company_projects()
    users = _company_users()
    if request.method == "POST":
        try:
            pid_raw = request.form.get("project_id") or None
            pid = int(pid_raw) if pid_raw else None
            project = None
            if pid:
                project = db.session.get(Project, pid)
                if not project or project.company_id != cid:
                    raise CRMError("المشروع غير موجود")

            assignee_ids = _parse_assignee_ids(request.form)
            if not assignee_ids:
                raise CRMError("يجب اختيار مكلَّف واحد على الأقل")

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
                assigned_to_id=assignee_ids[0],
                created_by_id=current_user.id,
                priority=TaskPriority[priority_str],
                status=TaskStatus.TODO,
                deadline=_parse_date(request.form.get("deadline")),
                notes=(request.form.get("notes") or "").strip() or None,
            )
            if not t.title:
                raise CRMError("عنوان المهمة مطلوب")
            db.session.add(t)
            db.session.flush()  # need t.id for the assignee rows

            set_assignees(t, assignee_ids, actor_id=current_user.id)
            log_activity(t, "CREATED",
                         after={"title": t.title, "ids": assignee_ids},
                         user_id=current_user.id)
            if project:
                project.recompute_progress()
            db.session.commit()

            # MARSOUD — multi-attachment upload at creation. Each file
            # goes through save_document() (same path the lead/project
            # uploads use), so size + extension validation is shared.
            # Failures on individual files don't roll back the task.
            uploaded = 0
            failed = []
            files = [fs for fs in request.files.getlist("attachments")
                     if fs and fs.filename]
            if files:
                from app.services.opsflow_extras import (
                    save_document, DocumentError,
                )
                from app.models import DocumentSourceType, DocumentVisibility
                for fs in files:
                    try:
                        save_document(
                            company_id=cid,
                            source_type=DocumentSourceType.TASK,
                            source_id=t.id,
                            file_storage=fs,
                            visibility=DocumentVisibility.INTERNAL,
                            uploaded_by_id=current_user.id,
                        )
                        uploaded += 1
                    except DocumentError as e:
                        failed.append(f"{fs.filename}: {e}")

            msg = f"تم إنشاء المهمة: {t.title}"
            if uploaded:
                msg += f" + {uploaded} مرفق"
            flash(msg, "success")
            for f in failed:
                flash(f"⚠ تعذّر رفع: {f}", "warning")
            return redirect(_safe_next(
                url_for("tasks.detail", task_id=t.id)))
        except (CRMError, TaskError, ValueError, TypeError, KeyError) as e:
            db.session.rollback()
            flash(str(e), "error")
    selected_project = request.args.get("project_id")
    # Map every project → its milestones so the form's milestone <select>
    # can swap options the instant the user picks a project. Without this
    # the dropdown only shows "— بدون —" because at GET time we don't
    # know which project they'll choose.
    milestones_by_project = {
        p.id: [{"id": m.id, "name": m.name} for m in p.milestones]
        for p in projects
    }
    initial_milestones = []
    if selected_project:
        try:
            initial_milestones = milestones_by_project.get(
                int(selected_project), [])
        except (TypeError, ValueError):
            initial_milestones = []
    return render_template("tasks/form.html",
                           task=None, projects=projects, users=users,
                           priorities=TaskPriority,
                           milestones=initial_milestones,
                           milestones_by_project=milestones_by_project,
                           selected_project=selected_project,
                           selected_assignee_ids=[])


@bp.route("/<int:task_id>")
@login_required
@require_permission("tasks.view")
def detail(task_id):
    t = _task_or_403(task_id)
    from app.services.opsflow_extras import documents_for
    from app.services.tasks_extras import activity_description
    docs = documents_for("TASK", t.id)
    role = _role()
    can_edit = _has_full_task_visibility() or current_user.id in assignee_ids_for(t) \
        or (role == "project_manager"
            and t.project_id in set(_pm_project_ids()))
    return render_template(
        "tasks/detail.html",
        task=t, statuses=TaskStatus, priorities=TaskPriority,
        docs=docs, can_edit=can_edit,
        users=_company_users(),
        current_assignee_ids=assignee_ids_for(t),
        activity_description=activity_description,
    )


@bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("tasks.manage")
def edit(task_id):
    t = _task_or_403(task_id)
    projects = _company_projects()
    users = _company_users()
    if request.method == "POST":
        try:
            new_title = (request.form.get("title") or t.title).strip()
            new_desc = (request.form.get("description") or "").strip() or None
            milestone_raw = request.form.get("milestone_id") or None
            new_milestone = int(milestone_raw) if milestone_raw else None
            priority_str = request.form.get("priority", t.priority.value)
            new_priority = TaskPriority[priority_str]
            new_deadline = _parse_date(request.form.get("deadline"))

            ids = _parse_assignee_ids(request.form)
            if not ids:
                raise TaskError("يجب اختيار مكلَّف واحد على الأقل")

            if new_title != t.title:
                log_activity(t, "TITLE_CHANGED",
                             before={"title": t.title},
                             after={"title": new_title})
                t.title = new_title
            if new_desc != t.description:
                if _can_edit_description(t):
                    log_activity(t, "DESCRIPTION_CHANGED",
                                 before={"description": (t.description or "")[:200]},
                                 after={"description": (new_desc or "")[:200]})
                    t.description = new_desc
                else:
                    flash("لا يمكن تعديل الوصف — هذا الحقل محجوز لمن أنشأ المهمة فقط.",
                          "warning")
            if new_priority != t.priority:
                log_activity(t, "PRIORITY_CHANGED",
                             before={"priority": t.priority.value},
                             after={"priority": new_priority.value})
                t.priority = new_priority
            if new_deadline != t.deadline:
                log_activity(t, "DEADLINE_CHANGED",
                             before={"deadline": str(t.deadline) if t.deadline else None},
                             after={"deadline": str(new_deadline) if new_deadline else None})
                t.deadline = new_deadline
            t.milestone_id = new_milestone
            t.notes = (request.form.get("notes") or "").strip() or None

            db.session.flush()
            set_assignees(t, ids, actor_id=current_user.id)
            db.session.commit()

            flash("تم حفظ التعديلات", "success")
            return redirect(_safe_next(
                url_for("tasks.detail", task_id=t.id)))
        except (TaskError, ValueError, TypeError, KeyError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("tasks/form.html",
                           task=t, projects=projects, users=users,
                           priorities=TaskPriority,
                           milestones=t.project.milestones if t.project else [],
                           selected_assignee_ids=list(assignee_ids_for(t)))


@bp.route("/<int:task_id>/status", methods=["POST"])
@login_required
@require_permission("tasks.view")
def status(task_id):
    t = _task_or_403(task_id)
    try:
        new_status = request.form.get("new_status")
        try:
            new_enum = TaskStatus[new_status]
        except (KeyError, TypeError):
            raise CRMError("حالة غير صالحة")
        if new_enum != t.status:
            log_activity(t, "STATUS_CHANGED",
                         before={"status": t.status.value},
                         after={"status": new_enum.value},
                         user_id=current_user.id)
        set_task_status(t, new_status, by_user_id=current_user.id)
        flash(f"تم تحديث الحالة إلى: {t.status.label_ar}", "success")
    except CRMError as e:
        flash(str(e), "error")
    # MARSOUD-TASK-CONTEXT — the legacy `return_to=kanban` sentinel
    # kept meaning "go back to /tasks/". _safe_next() now also
    # accepts a full URL path so the Kanban card status form can
    # send the exact scoped URL (e.g. /tasks/?scope=employees&user_id=15)
    # and land the user back inside the same drill-down.
    rt = request.form.get("return_to", "")
    if rt == "kanban":
        return redirect(url_for("tasks.index"))
    return redirect(_safe_next(url_for("tasks.detail", task_id=t.id)))


# ─── MARSOUD-67 + PERM-FIX (PM scope): full task delete (owner/admin) ──
@bp.route("/<int:task_id>/delete", methods=["POST"])
@login_required
@require_permission("tasks.delete")
def delete(task_id):
    """Hard-delete a task + all its dependents in one transaction:
    attachments (files on disk + documents rows), comments, activity
    log, assignee m2m rows, then the task row itself. Gated to
    owner/admin via the dedicated `tasks.delete` permission so a
    project_manager assigned to a task cannot delete it — only edit."""
    t = _task_or_403(task_id)
    try:
        delete_task_fully(t)
        flash("تم حذف المهمة وكل ما يتعلق بها", "success")
    except Exception as e:
        from flask import current_app
        current_app.logger.exception("delete_task_fully failed for %s", task_id)
        flash(f"تعذّر حذف المهمة: {e}", "error")
        return redirect(url_for("tasks.detail", task_id=task_id))
    # MARSOUD-TASK-CONTEXT — return_to lands the user back on whichever
    # list they came from (employee drill-down, project detail, etc)
    # instead of forcing them to the default 'mine' Kanban.
    return redirect(_safe_next(url_for("tasks.index")))


# ─── MARSOUD-TASK-ARCHIVE-01: archive lifecycle ──────────────────────────
@bp.route("/<int:task_id>/archive", methods=["POST"])
@login_required
@require_permission("tasks.archive")
def archive(task_id):
    """Soft-archive a single task. Owner/admin only."""
    from app.services.task_archive import archive_task
    t = _task_or_403(task_id)
    if archive_task(t, actor_id=current_user.id):
        flash(f"تم أرشفة المهمة: {t.title}", "success")
    else:
        flash("المهمة مؤرشفة بالفعل", "info")
    return redirect(request.referrer or url_for("tasks.index"))


@bp.route("/archive-all-done", methods=["POST"])
@login_required
@require_permission("tasks.archive")
def archive_all_done():
    """Archive every DONE + non-archived task in the company."""
    from app.services.task_archive import archive_all_done_in_company
    n = archive_all_done_in_company(g.active_company.id,
                                     actor_id=current_user.id)
    flash(f"تم أرشفة {n} مهمة مكتملة", "success")
    return redirect(request.referrer or url_for("tasks.index"))


@bp.route("/<int:task_id>/unarchive", methods=["POST"])
@login_required
@require_permission("tasks.archive")
def unarchive(task_id):
    """Restore an archived task to the board."""
    from app.services.task_archive import unarchive_task
    t = db.session.get(Task, task_id)
    if not t or t.company_id != g.active_company.id:
        abort(404)
    if unarchive_task(t):
        flash(f"تم استعادة المهمة: {t.title}", "success")
    else:
        flash("المهمة ليست مؤرشفة", "info")
    return redirect(request.referrer or url_for("tasks.archive_list"))


@bp.route("/archive", methods=["GET"])
@login_required
@require_permission("tasks.archive")
def archive_list():
    """Read-only listing of archived tasks. Owner/admin only."""
    archived = Task.query.filter(
        Task.company_id == g.active_company.id,
        Task.archived_at.isnot(None),
    ).order_by(Task.archived_at.desc()).all()
    return render_template("tasks/archive.html", tasks=archived)


# ─── Inline field edits (AJAX form posts) ────────────────────────────────
@bp.route("/<int:task_id>/inline", methods=["POST"])
@login_required
@require_permission("tasks.view")
def inline_edit(task_id):
    t = _task_or_403(task_id)
    desc_in = request.form.get("description")
    # MARSOUD — description is creator-only. Silently drop the submitted
    # value when a non-creator sends it (the editor form is hidden in the
    # template, but defence-in-depth covers direct POSTs).
    if desc_in is not None and not _can_edit_description(t):
        desc_in = None
        flash("لا يمكن تعديل الوصف — هذا الحقل محجوز لمن أنشأ المهمة فقط.",
              "warning")
    try:
        apply_inline_edit(
            t,
            title=request.form.get("title"),
            description=desc_in,
            priority=request.form.get("priority"),
            deadline=request.form.get("deadline"),
            status=request.form.get("status"),
            user_id=current_user.id,
        )
        flash("تم الحفظ", "success")
    except TaskError as e:
        flash(str(e), "error")
    return redirect(url_for("tasks.detail", task_id=task_id))


@bp.route("/<int:task_id>/assignees", methods=["POST"])
@login_required
@require_permission("tasks.view")
def update_assignees(task_id):
    t = _task_or_403(task_id)
    ids = _parse_assignee_ids(request.form)
    try:
        set_assignees(t, ids, actor_id=current_user.id)
        flash("تم تحديث المكلَّفين", "success")
    except TaskError as e:
        flash(str(e), "error")
    return redirect(url_for("tasks.detail", task_id=task_id))


# ─── Comments ─────────────────────────────────────────────────────────────
@bp.route("/<int:task_id>/comments", methods=["POST"])
@login_required
@require_permission("tasks.view")
def comment_add(task_id):
    t = _task_or_403(task_id)
    try:
        add_comment(t, request.form.get("content"), user_id=current_user.id)
        flash("تم إضافة التعليق", "success")
    except TaskError as e:
        flash(str(e), "error")
    return redirect(url_for("tasks.detail", task_id=task_id) + "#comments")


# ─── Team statistics ─────────────────────────────────────────────────────
@bp.route("/stats")
@login_required
@require_permission("tasks.view")
def stats():
    """MARSOUD — analytics-grade team-stats page.

    Query string:
      range=7|30|90|all  — restricts the count buckets to tasks created
                           within that window (velocity_30d ignores this).
    """
    from datetime import datetime, timedelta
    role = _role()
    cid = g.active_company.id

    range_arg = (request.args.get("range") or "all").lower()
    days_map = {"7": 7, "30": 30, "90": 90}
    since = None
    if range_arg in days_map:
        since = datetime.utcnow() - timedelta(days=days_map[range_arg])

    data = team_stats(cid, since=since)
    rows = data["rows"]
    if not _has_full_task_visibility() and role != "project_manager":
        rows = [r for r in rows if r["user"] and r["user"].id == current_user.id]
    return render_template(
        "tasks/stats.html",
        rows=rows,
        closed_per_week=data["closed_per_week"],
        status_dist=data["status_dist"],
        role=role, today=date.today(),
        range_arg=range_arg,
    )
