"""MARSOUD-API-V1 — JSON API for Ibrahim + Claude Code.

All endpoints under `/api/v1/`. Auth is bearer-token only (see
`@login_manager.request_loader` in app/__init__.py). Every response is
JSON; every error is `{"error": "..."}` with the right status code.

Helpers reused from the existing codebase (no logic duplication):
  - tasks_extras.visible_tasks_query / assignee_ids_for / add_comment
  - crm.set_task_status
  - permissions.has_permission

Files under /static/docs are served through `/api/v1/documents/<id>/download`
so the API enforces company + visibility checks instead of relying on the
URL path to act as a secret.
"""
import json
from datetime import date, datetime
from pathlib import Path

from flask import (
    Blueprint, jsonify, request, g, current_app, send_file, abort,
)
from flask_login import current_user
from sqlalchemy import or_

from app import db
from app.models import (
    Project, ProjectStatus, Task, TaskStatus, TaskPriority,
    TaskComment, TaskActivityLog, Document, User, Company,
    task_assignees,
)
from app.services.tasks_extras import (
    add_comment, visible_tasks_query, assignee_ids_for, is_visible_to,
    TaskError,
)
from app.services.crm import set_task_status, CRMError
from app.services.permissions import has_permission

bp = Blueprint("api_v1", __name__)


# ─── Error helpers ──────────────────────────────────────────────────────
def _err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    resp = jsonify(payload)
    resp.status_code = status
    return resp


@bp.errorhandler(404)
def _e404(e):
    return _err("not found", 404)


@bp.errorhandler(405)
def _e405(e):
    return _err("method not allowed", 405)


@bp.errorhandler(500)
def _e500(e):
    return _err("internal error", 500)


# ─── Auth guard ─────────────────────────────────────────────────────────
@bp.before_request
def _require_api_token():
    """Every endpoint requires a valid bearer token.

    MARSOUD-PLAN-SSOT (2026-08-17) — trusts the Authorization header
    as the sole source of truth for bearer identity, never
    `current_user` alone. Flask 2+ binds `flask.g` to the app_context,
    not the request context; a test harness that pushes an app_context
    and then makes multiple test_client requests (or a session-cookie
    login in one call followed by a bearer call in the next) would
    see `g._login_user` leak between requests and falsely authorize
    the bearer call. Verifying the bearer header fresh on every
    request kills that leak.

    Company resolution:
      - `?company_id=N` query param → use that company if the caller is
        a member; 403 otherwise.
      - Otherwise → first company in the token owner's companies.
    """
    # ── Fresh bearer verify ──────────────────────────────────────
    auth = request.headers.get("Authorization", "")
    tok = None
    if auth.startswith("Bearer "):
        raw = auth[7:].strip()
        if raw:
            try:
                from app.services.api_tokens import verify_token
                tok = verify_token(raw)
            except Exception:
                tok = None
    if tok is None:
        return _err("missing or invalid bearer token", 401)
    if not tok.user or not tok.user.is_active:
        return _err("inactive user", 401)

    # ── Load current_user for the view, without polluting session ──
    from flask_login import login_user
    if not (current_user.is_authenticated
            and getattr(current_user, "id", None) == tok.user_id):
        # Directly install on g — do NOT call login_user() with force,
        # which writes session["_user_id"] and lets a proxy leak the
        # bearer-authenticated session to future non-bearer requests.
        g._login_user = tok.user

    # ── Always re-resolve active company from the fresh user ──────
    companies = list(tok.user.companies)
    if not companies:
        return _err("user has no company", 403)
    cid_arg = request.args.get("company_id", type=int)
    if cid_arg:
        match = next((c for c in companies if c.id == cid_arg), None)
        if not match:
            return _err(
                f"you are not a member of company {cid_arg}", 403)
        g.active_company = match
    else:
        g.active_company = companies[0]

    # ── Rate limit — reuse the token we already verified. ─────────
    token_id = tok.id
    if token_id is not None:
        from app.services.rate_limit import check_and_increment
        ok, retry_after = check_and_increment(token_id)
        if not ok:
            # Log for forensics (ticket asks for this explicitly).
            from flask import current_app
            try:
                current_app.logger.warning(
                    "api rate-limit exceeded: token=%s company=%s "
                    "endpoint=%s ip=%s retry_after=%ss",
                    token_id,
                    g.active_company.id if g.get("active_company") else None,
                    request.endpoint, request.remote_addr,
                    retry_after,
                )
            except Exception:
                pass
            resp = jsonify({
                "success": False,
                "message": "Rate limit exceeded. Please try again later.",
                "retry_after_seconds": retry_after,
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after)
            return resp


# ─── Serializers ────────────────────────────────────────────────────────
def _iso(dt):
    return dt.isoformat() if dt else None


def _user_brief(u):
    if not u:
        return None
    return {"id": u.id, "name": u.full_name, "email": u.email}


def _project_brief(p):
    if not p:
        return None
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status.value if p.status else None,
    }


def _task_counts_for_project(project_id):
    rows = db.session.query(Task.status, db.func.count(Task.id)).filter(
        Task.project_id == project_id,
    ).group_by(Task.status).all()
    out = {s.value.lower(): 0 for s in TaskStatus}
    for status, n in rows:
        out[status.value.lower()] = n
    return out


def _project_full(p):
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status.value if p.status else None,
        "type": p.type,
        "manager": _user_brief(p.manager),
        "customer": ({"id": p.customer.id, "name": p.customer.name}
                     if p.customer else None),
        "progress_pct": float(p.progress_pct or 0),
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "notes": p.notes,
        "task_counts": _task_counts_for_project(p.id),
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


def _task_brief(t):
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status.value if t.status else None,
        "priority": t.priority.value if t.priority else None,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "project": _project_brief(t.project),
        "is_overdue": t.is_overdue,
        "assignee_count": len(assignee_ids_for(t)),
        "created_at": _iso(t.created_at),
    }


def _attachment_brief(doc):
    return {
        "id": doc.id,
        "name": doc.name,
        "size_bytes": doc.size_bytes,
        "mimetype": doc.mimetype,
        "download_url": f"/api/v1/documents/{doc.id}/download",
        "uploaded_by": (_user_brief(doc.uploaded_by)
                         if doc.uploaded_by else None),
        "created_at": _iso(doc.created_at),
    }


def _task_full(t):
    assignee_ids = assignee_ids_for(t)
    users_map = {u.id: u for u in
                 User.query.filter(User.id.in_(assignee_ids)).all()}
    attachments = Document.query.filter_by(
        source_type="TASK", source_id=t.id,
    ).order_by(Document.created_at.desc()).all()
    comments = sorted(t.comments, key=lambda c: c.created_at)
    activity = sorted(t.activity, key=lambda a: a.created_at, reverse=True)
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status.value if t.status else None,
        "priority": t.priority.value if t.priority else None,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "notes": t.notes,
        "project": _project_brief(t.project),
        "milestone": ({"id": t.milestone.id, "name": t.milestone.name}
                       if t.milestone else None),
        "created_by": _user_brief(t.created_by),
        "assignees": [_user_brief(users_map[uid])
                       for uid in assignee_ids if uid in users_map],
        "is_overdue": t.is_overdue,
        "comments": [{
            "id": c.id,
            "user": _user_brief(c.user),
            "content": c.content,
            "attachment_url": c.attachment_url,
            "attachment_name": c.attachment_name,
            "created_at": _iso(c.created_at),
        } for c in comments],
        "activity": [{
            "id": a.id,
            "action": a.action,
            "user": _user_brief(a.user),
            "before": json.loads(a.before_json) if a.before_json else None,
            "after": json.loads(a.after_json) if a.after_json else None,
            "created_at": _iso(a.created_at),
        } for a in activity],
        "attachments": [_attachment_brief(d) for d in attachments],
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
        "completed_at": _iso(t.completed_at),
    }


# ─── Visibility helpers ─────────────────────────────────────────────────
def _full_task_visibility():
    return has_permission("tasks.view_all") or (
        has_permission("tasks.manage") and getattr(current_user, "id", 0) > 0
        and _role_in(("owner", "admin"))
    )


def _role_in(roles):
    from app.services.permissions import get_user_role
    return get_user_role(current_user.id, g.active_company.id) in roles


def _pm_project_ids():
    return [r[0] for r in db.session.query(Project.id).filter(
        Project.company_id == g.active_company.id,
        Project.manager_id == current_user.id,
    ).all()]


# ─── Endpoints ──────────────────────────────────────────────────────────
@bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "ok": True,
        "user": _user_brief(current_user),
        "company": {"id": g.active_company.id,
                    "name": g.active_company.name},
        "server_time": _iso(datetime.utcnow()),
    })


@bp.route("/me/companies", methods=["GET"])
def my_companies():
    """List every company the token holder is a member of, with the
    role in each. Use this to discover which company_id to pass on
    subsequent calls (e.g. /api/v1/projects?company_id=N)."""
    from app.models.user import user_companies
    from app.models import Company
    rows = db.session.execute(
        user_companies.select().where(
            user_companies.c.user_id == current_user.id,
        )
    ).fetchall()
    out = []
    for r in rows:
        c = db.session.get(Company, r.company_id)
        if not c:
            continue
        out.append({
            "id": c.id,
            "name": c.name,
            "role": r.role,
            "is_active_company_for_request": (g.active_company
                                                and g.active_company.id == c.id),
        })
    return jsonify({"count": len(out), "companies": out})


@bp.route("/me/tasks", methods=["GET"])
def my_tasks_global():
    """MARSOUD — every task in the system where the token holder is the
    primary assignee (assigned_to_id) OR a multi-assignee member.
    Scope intentionally crosses company boundaries — if Abdelhamid in
    company A assigned a ticket to ibrahimfakhreyams@gmail.com, this
    surfaces it even when Ibrahim isn't a member of company A.

    Useful for "show me everything I owe" dashboards. To get the full
    detail (comments / attachments / activity) follow up with
    `/api/v1/tasks/<id>?company_id=<task_company_id>` — the response
    includes the company_id for each row so you know which to use.

    Query params:
      status=...           optional filter (TODO / IN_PROGRESS / REVIEW /
                            DONE / BLOCKED)
      include_archived=1   include archived rows too (default: exclude)
      limit=N              cap at 200
    """
    from app.models import Task, TaskStatus, task_assignees as _ta, Company
    status_arg = (request.args.get("status") or "").strip()
    include_archived = request.args.get("include_archived", "0") == "1"
    limit = min(int(request.args.get("limit", 100) or 100), 200)

    # Build the OR(primary, multi-assignee) base, no company filter.
    sub = db.session.query(_ta.c.task_id).filter(
        _ta.c.user_id == current_user.id,
    )
    q = Task.query.filter(or_(
        Task.assigned_to_id == current_user.id,
        Task.id.in_(sub),
    ))
    if not include_archived:
        q = q.filter(Task.archived_at.is_(None))
    if status_arg:
        try:
            q = q.filter(Task.status == TaskStatus[status_arg])
        except KeyError:
            return _err(f"unknown status: {status_arg}", 400)
    tasks = q.order_by(Task.deadline.asc().nullslast(),
                        Task.created_at.desc()).limit(limit).all()
    # Resolve every distinct company in one go for the response payload.
    cid_set = {t.company_id for t in tasks}
    companies = {c.id: c for c in
                  Company.query.filter(Company.id.in_(cid_set)).all()}

    def _row(t):
        c = companies.get(t.company_id)
        return {
            "id": t.id,
            "title": t.title,
            "status": t.status.value if t.status else None,
            "priority": t.priority.value if t.priority else None,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "is_overdue": t.is_overdue,
            "project": _project_brief(t.project),
            # Critical for cross-company follow-ups: tells the caller
            # which company_id to pass when fetching task detail.
            "company": ({"id": c.id, "name": c.name} if c else
                         {"id": t.company_id, "name": None}),
            "created_by": _user_brief(t.created_by),
            "created_at": _iso(t.created_at),
            "archived_at": _iso(t.archived_at),
        }
    return jsonify({
        "count": len(tasks),
        "tasks": [_row(t) for t in tasks],
    })


@bp.route("/me", methods=["GET"])
def me():
    from app.services.permissions import get_user_role, P, _IMPLIES
    from app.services.plan_snapshot import plan_snapshot
    role = get_user_role(current_user.id, g.active_company.id)
    # List every permission this user has (via DB-backed role + legacy)
    perms = sorted([code for code in P
                    if has_permission(code)])
    # MARSOUD-PLAN-SSOT (2026-08-17) — include the plan snapshot so
    # mobile clients + third-party integrations read the plan from
    # the SAME single source of truth the web uses. Trial is a
    # subscription STATUS, not a plan — never returns "Free".
    ps = plan_snapshot(g.active_company)
    return jsonify({
        "user": _user_brief(current_user),
        "company": {"id": g.active_company.id,
                    "name": g.active_company.name,
                    "base_currency": g.active_company.base_currency},
        "role": role,
        "permissions": perms,
        "subscription": {
            "plan_code":    ps["plan_code"],
            "plan_name_ar": ps["plan_name_ar"],
            "status":       ps["status"],
            "access_mode":  ps["access_mode"],
            "trial_ends_at": _iso(ps["trial_ends_at"]),
            "subscription_started_at": _iso(ps["subscription_started_at"]),
            "subscription_expires_at": _iso(ps["subscription_expires_at"]),
            "warning_days_left": ps["warning_days_left"],
        },
    })


@bp.route("/projects", methods=["GET"])
def projects_list():
    q = (request.args.get("q") or "").strip()
    status_arg = (request.args.get("status") or "").strip()
    limit = min(int(request.args.get("limit", 20) or 20), 100)

    query = Project.query.filter(Project.company_id == g.active_company.id)
    if q:
        query = query.filter(Project.name.ilike(f"%{q}%"))
    if status_arg:
        try:
            query = query.filter(Project.status == ProjectStatus[status_arg])
        except KeyError:
            return _err(f"unknown status: {status_arg}", 400)
    projects = query.order_by(Project.created_at.desc()).limit(limit).all()
    return jsonify({
        "count": len(projects),
        "projects": [_project_full(p) for p in projects],
    })


@bp.route("/projects/<int:project_id>", methods=["GET"])
def project_detail(project_id):
    p = db.session.get(Project, project_id)
    if not p or p.company_id != g.active_company.id:
        return _err("project not found", 404)
    return jsonify({"project": _project_full(p)})


@bp.route("/projects/<int:project_id>/tasks", methods=["GET"])
def project_tasks(project_id):
    p = db.session.get(Project, project_id)
    if not p or p.company_id != g.active_company.id:
        return _err("project not found", 404)

    assigned_to_me = (
        (request.args.get("assigned_to_me") or "true").lower() == "true"
    )
    status_arg = (request.args.get("status") or "").strip()
    limit = min(int(request.args.get("limit", 50) or 50), 200)

    if assigned_to_me:
        # Always scope to current user, regardless of view_all.
        q = visible_tasks_query(
            g.active_company.id, current_user.id, full_visibility=False,
            pm_project_ids=None,
        )
    else:
        # Only allow listing all when the user actually has view_all.
        if not has_permission("tasks.view_all"):
            return _err("assigned_to_me=false requires tasks.view_all", 403)
        q = Task.query.filter(Task.company_id == g.active_company.id)

    q = q.filter(Task.project_id == project_id)
    if status_arg:
        try:
            q = q.filter(Task.status == TaskStatus[status_arg])
        except KeyError:
            return _err(f"unknown status: {status_arg}", 400)
    tasks = q.order_by(Task.deadline.asc().nullslast(),
                       Task.created_at.desc()).limit(limit).all()
    return jsonify({
        "project": _project_brief(p),
        "count": len(tasks),
        "tasks": [_task_brief(t) for t in tasks],
    })


@bp.route("/tasks/<int:task_id>", methods=["GET"])
def task_detail(task_id):
    t = db.session.get(Task, task_id)
    if not t or t.company_id != g.active_company.id:
        return _err("task not found", 404)
    full = has_permission("tasks.view_all")
    pm_pids = _pm_project_ids() if _role_in(("project_manager",)) else None
    if not is_visible_to(t, current_user.id, full, pm_pids):
        return _err("task not found", 404)
    return jsonify({"task": _task_full(t)})


@bp.route("/tasks/search", methods=["GET"])
def tasks_search():
    q = (request.args.get("q") or "").strip()
    project_id = request.args.get("project_id", type=int)
    limit = min(int(request.args.get("limit", 20) or 20), 100)
    if not q:
        return _err("q is required", 400)
    full = has_permission("tasks.view_all")
    pm_pids = _pm_project_ids() if _role_in(("project_manager",)) else None
    base = visible_tasks_query(
        g.active_company.id, current_user.id, full, pm_pids,
    )
    base = base.filter(Task.title.ilike(f"%{q}%"))
    if project_id:
        base = base.filter(Task.project_id == project_id)
    tasks = base.order_by(Task.created_at.desc()).limit(limit).all()
    return jsonify({
        "count": len(tasks),
        "tasks": [_task_brief(t) for t in tasks],
    })


@bp.route("/tasks/<int:task_id>/status", methods=["POST"])
def task_status(task_id):
    t = db.session.get(Task, task_id)
    if not t or t.company_id != g.active_company.id:
        return _err("task not found", 404)
    full = has_permission("tasks.view_all")
    pm_pids = _pm_project_ids() if _role_in(("project_manager",)) else None
    if not is_visible_to(t, current_user.id, full, pm_pids):
        return _err("task not found", 404)
    if not has_permission("tasks.manage") and current_user.id not in assignee_ids_for(t):
        return _err("not permitted to change this task's status", 403)
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip()
    if not new_status:
        return _err("status is required", 400)
    if new_status not in {s.value for s in TaskStatus}:
        return _err(f"unknown status: {new_status}", 400)
    try:
        set_task_status(t, new_status, by_user_id=current_user.id)
    except CRMError as e:
        return _err(str(e), 400)
    return jsonify({"task": _task_full(t)})


@bp.route("/tasks/<int:task_id>/comments", methods=["POST"])
def task_comment(task_id):
    t = db.session.get(Task, task_id)
    if not t or t.company_id != g.active_company.id:
        return _err("task not found", 404)
    full = has_permission("tasks.view_all")
    pm_pids = _pm_project_ids() if _role_in(("project_manager",)) else None
    if not is_visible_to(t, current_user.id, full, pm_pids):
        return _err("task not found", 404)
    body = request.get_json(silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        return _err("content is required", 400)
    try:
        c = add_comment(t, content, user_id=current_user.id)
    except TaskError as e:
        return _err(str(e), 400)
    return jsonify({
        "comment": {
            "id": c.id,
            "user": _user_brief(c.user),
            "content": c.content,
            "created_at": _iso(c.created_at),
        },
    })


@bp.route("/documents/<int:doc_id>/download", methods=["GET"])
def document_download(doc_id):
    doc = db.session.get(Document, doc_id)
    # 404 on any mismatch — never leak existence.
    if not doc or doc.company_id != g.active_company.id:
        return _err("document not found", 404)
    # Confirm parent visibility.
    if doc.source_type == "TASK":
        t = db.session.get(Task, doc.source_id)
        if not t or t.company_id != g.active_company.id:
            return _err("document not found", 404)
        full = has_permission("tasks.view_all")
        pm_pids = _pm_project_ids() if _role_in(("project_manager",)) else None
        if not is_visible_to(t, current_user.id, full, pm_pids):
            return _err("document not found", 404)
    elif doc.source_type == "PROJECT":
        p = db.session.get(Project, doc.source_id)
        if not p or p.company_id != g.active_company.id:
            return _err("document not found", 404)
        if not has_permission("projects.view"):
            return _err("document not found", 404)
    elif doc.source_type == "LEAD":
        from app.models import Lead
        L = db.session.get(Lead, doc.source_id)
        if not L or L.company_id != g.active_company.id:
            return _err("document not found", 404)
        if not has_permission("leads.view"):
            return _err("document not found", 404)
    else:
        return _err("document not found", 404)

    # `file_path` is stored like "/static/docs/<cid>/task/<file>". Resolve
    # to a real disk path under app.root_path. Reject any path-traversal
    # attempt that escapes the static dir.
    rel = (doc.file_path or "").lstrip("/")
    if not rel.startswith("static/"):
        return _err("document not found", 404)
    disk = (Path(current_app.root_path) / rel).resolve()
    static_root = (Path(current_app.root_path) / "static").resolve()
    try:
        disk.relative_to(static_root)
    except ValueError:
        return _err("document not found", 404)
    if not disk.exists():
        return _err("document not found", 404)
    return send_file(
        str(disk), mimetype=doc.mimetype or "application/octet-stream",
        as_attachment=True, download_name=doc.name or disk.name,
    )
