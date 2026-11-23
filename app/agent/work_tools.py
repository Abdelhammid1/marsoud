"""MARSOUD-P116 (T-024) — أدوات المهام للمساعدين (فهيم أفندي وابن الشركة).

أداتين كتابة (create_tasks / update_task) بتعدّي دايمًا على اقتراح + تأكيد،
و4 أدوات قراءة (search_tasks / list_company_users / list_projects /
list_milestones).

التنفيذ الفعلي بيحصل بس من execute_proposal (services/agent_safety.py) بعد
ما المستخدم يضغط «تأكيد ونفّذ». أي _confirmed_proposal_id جاي من الموديل
نفسه بيترفض (الـ flag اللي بيفتح التنفيذ بيتحط على g من execute_proposal بس).
"""
import hashlib
import html as _html
import re
from datetime import datetime, timedelta

from flask import g, current_app

from app import db

WORK_WRITE_TOOLS = frozenset({"create_tasks", "update_task"})
WORK_READ_TOOLS = frozenset({"search_tasks", "list_company_users",
                             "list_projects", "list_milestones"})
WORK_TOOL_NAMES = WORK_WRITE_TOOLS | WORK_READ_TOOLS
MAX_BULK = 10
_PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")
_STATUSES = ("TODO", "IN_PROGRESS", "REVIEW", "DONE", "BLOCKED")


class _WorkError(Exception):
    """رسالة عربية مفهومة للمستخدم (بتترجّع للموديل كـ error)."""


# ─── Schemas ────────────────────────────────────────────────────────────
_TASK_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "عنوان المهمة (إجباري، حتى 200 حرف)"},
        "description": {"type": "string",
                        "description": "وصف المهمة نص عادي. أي وقت محدد أو عميل أو فاتورة أو منتج يتكتب هنا كنص."},
        "assignee_ids": {"type": "array", "items": {"type": "integer"},
                         "description": "أرقام المسؤولين من list_company_users (إجباري، واحد على الأقل)"},
        "deadline": {"type": "string",
                     "description": "تاريخ الاستحقاق بصيغة YYYY-MM-DD (اختياري، بدون وقت)"},
        "priority": {"type": "string",
                     "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                     "description": "الأولوية، الافتراضي MEDIUM"},
        "project_id": {"type": "integer",
                       "description": "رقم المشروع من list_projects (اختياري)"},
        "milestone_id": {"type": "integer",
                         "description": "رقم المرحلة من list_milestones (اختياري، لازم مع project_id)"},
        "notes": {"type": "string", "description": "ملاحظات (اختياري)"},
    },
    "required": ["title"],
}

WORK_TOOL_SCHEMAS = [
    {
        "name": "list_company_users",
        "description": ("يرجّع مستخدمي الشركة (الرقم والاسم) عشان تختار المسؤول "
                        "عن المهمة. استخدمها كل ما المستخدم يذكر اسم شخص."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "جزء من الاسم (اختياري)"}},
        },
    },
    {
        "name": "list_projects",
        "description": "يرجّع مشاريع الشركة (الرقم والاسم) لربط المهمة بمشروع.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "جزء من اسم المشروع (اختياري)"}},
        },
    },
    {
        "name": "list_milestones",
        "description": "يرجّع مراحل مشروع معيّن (الرقم والاسم).",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer",
                                          "description": "رقم المشروع"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "search_tasks",
        "description": ("بحث في المهام اللي المستخدم يقدر يشوفها فعلًا. "
                        "استخدمها قبل update_task عشان تعرف رقم المهمة."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "جزء من عنوان المهمة"},
                "status": {"type": "string",
                           "enum": ["TODO", "IN_PROGRESS", "REVIEW", "DONE", "BLOCKED"]},
                "assignee_id": {"type": "integer",
                                "description": "رقم مسؤول من list_company_users"},
                "overdue_only": {"type": "boolean",
                                 "description": "true لعرض المهام المتأخرة فقط (تاريخ استحقاقها فات ولم تكتمل)"},
                "due_within_days": {"type": "integer",
                                    "description": "عرض المهام المستحقة خلال هذا العدد من الأيام من اليوم (0 = اليوم فقط، 7 = خلال أسبوع)"},
                "limit": {"type": "integer",
                          "description": "عدد النتائج (الافتراضي 10، الأقصى 20)"},
            },
        },
    },
    {
        "name": "create_tasks",
        "description": ("تجهيز مهمة أو أكتر (حد أقصى 10) لإنشائها في إدارة العمل. "
                        "الأداة ما بتنشئش حاجة مباشرة: بترجّع «يحتاج تأكيد» "
                        "والمستخدم يضغط تأكيد من البطاقة. العنوان والمسؤول إجباريين: "
                        "لو ناقصين اسأل المستخدم ولا تخترع قيمة. تاريخ الاستحقاق "
                        "اختياري. لا تقل إن المهمة اتسجّلت قبل ما المستخدم يأكّد."),
        "input_schema": {
            "type": "object",
            "properties": {"tasks": {"type": "array", "items": _TASK_ITEM,
                                     "description": "قائمة المهام (حد أقصى 10)"}},
            "required": ["tasks"],
        },
    },
    {
        "name": "update_task",
        "description": ("تجهيز تعديل على مهمة موجودة (ابحث عنها بـ search_tasks "
                        "الأول). ابعت بس الحقول اللي المستخدم عاوز يغيّرها. "
                        "بترجّع «يحتاج تأكيد» بالقديم والجديد. وصف المهمة يتعدّل "
                        "بس من اللي أنشأها. مفيش حذف."),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "رقم المهمة"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee_ids": {"type": "array", "items": {"type": "integer"}},
                "priority": {"type": "string",
                             "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"]},
                "status": {"type": "string",
                           "enum": ["TODO", "IN_PROGRESS", "REVIEW", "DONE", "BLOCKED"]},
                "deadline": {"type": "string",
                             "description": "YYYY-MM-DD"},
                "clear_deadline": {"type": "boolean",
                                   "description": "true لمسح تاريخ الاستحقاق"},
                "project_id": {"type": "integer"},
                "milestone_id": {"type": "integer"},
                "notes": {"type": "string",
                          "description": "نص فاضي لمسح الملاحظات"},
            },
            "required": ["task_id"],
        },
    },
]


# ─── Helpers ────────────────────────────────────────────────────────────
def _sha(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _plain(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def _short(s, n=60):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _uname(u, uid):
    return (u.full_name if u is not None and u.full_name else f"مستخدم {uid}")


def _ctx(company_id, user_id, action):
    from app.models import Company, User
    from app.services.permissions import has_permission
    company = db.session.get(Company, company_id) if company_id else None
    user = db.session.get(User, user_id) if user_id else None
    if company is None or user is None or not getattr(user, "is_active", True):
        raise _WorkError("مفيش شركة أو مستخدم نشط للعملية دي")
    if not has_permission(action, user, company):
        raise _WorkError("مفيش عندك صلاحية على المهام في الشركة دي")
    return company, user


def _members(cid):
    from sqlalchemy import select
    from app.models import User
    from app.models.user import user_companies
    ids = [r[0] for r in db.session.execute(
        select(user_companies.c.user_id)
        .where(user_companies.c.company_id == cid)).fetchall()]
    if not ids:
        return {}
    return {u.id: u for u in User.query.filter(User.id.in_(ids)).all()}


def _visibility(company, user):
    from app.models import Project
    from app.services.permissions import has_permission, get_user_role
    role = get_user_role(user.id, company.id)
    full = bool(has_permission("tasks.view_all", user, company)) or role in ("owner", "admin")
    pm = None
    if role == "project_manager":
        pm = [r[0] for r in db.session.query(Project.id).filter(
            Project.company_id == company.id,
            Project.manager_id == user.id).all()]
    return full, pm


def _load_task(task_id, company, user):
    from app.models import Task
    from app.services.tasks_extras import is_visible_to
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise _WorkError("رقم المهمة غير صحيح")
    t = db.session.get(Task, tid)
    if t is None or t.company_id != company.id or t.archived_at is not None:
        raise _WorkError("المهمة مش موجودة")
    full, pm = _visibility(company, user)
    if not is_visible_to(t, user.id, full, pm):
        raise _WorkError("المهمة مش موجودة")
    return t


def _pdate(s, label):
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise _WorkError(f"{label}: التاريخ لازم يكون بصيغة YYYY-MM-DD")


def _desc_html(text):
    from app.services.rich_text import sanitize_html
    text = (text or "").strip()[:5000]
    if not text:
        return None
    paras = [p for p in text.replace("\r", "").split("\n\n") if p.strip()]
    h = "".join("<p>" + _html.escape(p.strip()).replace("\n", "<br>") + "</p>"
                for p in paras)
    return sanitize_html(h).strip() or None


def _empty(v):
    return v in (None, "", 0)


def _check_project(cid, project_id, milestone_id):
    from app.models import Project, Milestone
    project = None
    if not _empty(project_id):
        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            raise _WorkError("رقم المشروع غير صحيح")
        project = db.session.get(Project, pid)
        if (project is None or project.company_id != cid
                or project.deleted_at is not None):
            raise _WorkError("المشروع غير موجود")
    mid = None
    if not _empty(milestone_id):
        try:
            mid = int(milestone_id)
        except (TypeError, ValueError):
            raise _WorkError("رقم المرحلة غير صحيح")
        if project is None:
            raise _WorkError("لا يمكن ربط مرحلة بدون مشروع")
        m = db.session.get(Milestone, mid)
        if m is None or m.project_id != project.id:
            raise _WorkError("المرحلة لا تنتمي لهذا المشروع")
    return project, mid


def _assignees(cid, raw):
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise _WorkError("المسؤول ناقص — اسأل المستخدم مين يتكلّف بالمهمة "
                         "(استخدم list_company_users)")
    members = _members(cid)
    out = []
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise _WorkError("رقم مسؤول غير صحيح")
        if i not in members:
            raise _WorkError("أحد المسؤولين مش من مستخدمي الشركة")
        if i not in out:
            out.append(i)
    return out, members


# ─── Read tools ─────────────────────────────────────────────────────────
def _list_users(args, company):
    members = _members(company.id)
    q = str(args.get("query") or "").strip().casefold()[:60]
    rows = []
    for uid, u in members.items():
        if not getattr(u, "is_active", True):
            continue
        name = _uname(u, uid)
        if q and q not in name.casefold():
            continue
        rows.append({"id": uid, "name": name})
    rows.sort(key=lambda r: r["name"])
    return {"users": rows[:100], "count": len(rows[:100])}


def _list_projects(args, company):
    from app.models import Project
    q = Project.query.filter(Project.company_id == company.id,
                             Project.deleted_at.is_(None))
    text = str(args.get("query") or "").strip()[:60]
    if text:
        q = q.filter(Project.name.icontains(text, autoescape=True))
    rows = q.order_by(Project.name).limit(50).all()
    return {"projects": [{"id": p.id, "number": p.number, "name": p.name}
                         for p in rows], "count": len(rows)}


def _list_milestones(args, company):
    from app.models import Milestone
    project, _m = _check_project(company.id, args.get("project_id"), None)
    if project is None:
        raise _WorkError("رقم المشروع ناقص")
    rows = (Milestone.query.filter_by(project_id=project.id)
            .order_by(Milestone.order, Milestone.id).all())
    return {"milestones": [{"id": m.id, "name": m.name,
                            "target_date": m.target_date.isoformat() if m.target_date else None}
                           for m in rows], "count": len(rows)}


def _search_tasks(args, company, user):
    from sqlalchemy import select, or_
    from app.models import Task, TaskStatus, task_assignees
    from app.services.tasks_extras import visible_tasks_query, assignee_ids_for
    from app.services.url_ids import encode_id
    full, pm = _visibility(company, user)
    q = visible_tasks_query(company.id, user.id, full, pm).filter(
        Task.archived_at.is_(None))
    text = str(args.get("query") or "").strip()[:100]
    if text:
        q = q.filter(Task.title.icontains(text, autoescape=True))
    st = args.get("status")
    if st:
        s = str(st).upper()
        if s not in _STATUSES:
            raise _WorkError("الحالة غير معروفة")
        q = q.filter(Task.status == TaskStatus[s])
    if not _empty(args.get("assignee_id")):
        try:
            aid = int(args["assignee_id"])
        except (TypeError, ValueError):
            raise _WorkError("رقم المسؤول غير صحيح")
        q = q.filter(or_(
            Task.assigned_to_id == aid,
            Task.id.in_(select(task_assignees.c.task_id).where(
                task_assignees.c.user_id == aid))))
    from datetime import date as _date
    today = _date.today()
    if args.get("overdue_only") is True:
        q = q.filter(
            Task.deadline.isnot(None),
            Task.deadline < today,
            Task.status != TaskStatus.DONE,
        )
    elif args.get("due_within_days") is not None:
        try:
            days = max(int(args["due_within_days"]), 0)
        except (TypeError, ValueError):
            raise _WorkError("عدد الأيام غير صحيح")
        q = q.filter(
            Task.deadline.isnot(None),
            Task.deadline >= today,
            Task.deadline <= today + timedelta(days=days),
            Task.status != TaskStatus.DONE,
        )
    try:
        limit = min(max(int(args.get("limit") or 10), 1), 20)
    except (TypeError, ValueError):
        limit = 10
    order_col = Task.deadline.asc() if (args.get("overdue_only") is True or args.get("due_within_days") is not None) else Task.created_at.desc()
    rows = q.order_by(order_col, Task.created_at.desc()).limit(limit).all()
    members = _members(company.id)
    out = []
    for t in rows:
        is_over = bool(t.deadline and t.deadline < today and t.status != TaskStatus.DONE)
        out.append({
            "id": t.id, "title": t.title,
            "status": t.status.value, "status_ar": t.status.label_ar,
            "priority": t.priority.value,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "is_overdue": is_over,
            "assignees": [{"id": i, "name": _uname(members.get(i), i)}
                          for i in sorted(assignee_ids_for(t))],
            "project_id": t.project_id,
            "created_by_me": t.created_by_id == user.id,
            "link": "/tasks/" + encode_id(t.id),
        })
    return {"tasks": out, "count": len(out), "today": today.isoformat()}


# ─── Create ─────────────────────────────────────────────────────────────
def _normalize_create(args, company):
    from app.models import TaskPriority
    items = args.get("tasks")
    if not isinstance(items, list) or not items:
        raise _WorkError("لازم تبعت مهمة واحدة على الأقل في tasks")
    if len(items) > MAX_BULK:
        raise _WorkError(f"الحد الأقصى {MAX_BULK} مهام في المرة الواحدة")
    out = []
    for n, it in enumerate(items, 1):
        try:
            if not isinstance(it, dict):
                raise _WorkError("بيانات المهمة غير صحيحة")
            title = str(it.get("title") or "").strip()
            if not title:
                raise _WorkError("العنوان ناقص")
            if len(title) > 200:
                raise _WorkError("العنوان أطول من 200 حرف")
            ids, members = _assignees(company.id, it.get("assignee_ids"))
            dl = None
            if not _empty(it.get("deadline")):
                dl = _pdate(it["deadline"], "تاريخ الاستحقاق")
            pri = str(it.get("priority") or "MEDIUM").upper()
            if pri not in _PRIORITIES:
                raise _WorkError("الأولوية لازم تكون LOW أو MEDIUM أو HIGH أو URGENT")
            project, mid = _check_project(company.id, it.get("project_id"),
                                          it.get("milestone_id"))
            notes = str(it.get("notes") or "").strip()[:2000] or None
        except _WorkError as e:
            raise _WorkError(f"المهمة رقم {n}: {e}")
        out.append({
            "title": title, "desc": _desc_html(it.get("description")),
            "ids": ids, "dl": dl, "pri": pri, "notes": notes,
            "project_id": project.id if project else None,
            "project_name": project.name if project else None,
            "milestone_id": mid,
            "names": "، ".join(_uname(members.get(i), i) for i in ids),
        })
    return out


def _summary_create(norm):
    from app.models import TaskPriority
    lines = [f"إنشاء {len(norm)} مهمة:"]
    for n, it in enumerate(norm, 1):
        dl = it["dl"].isoformat() if it["dl"] else "بدون تاريخ استحقاق ⚠️"
        pj = f" — المشروع: {it['project_name']}" if it["project_name"] else ""
        lines.append(f"{n}) «{it['title']}» — المسؤول: {it['names']} — "
                     f"الاستحقاق: {dl} — الأولوية: "
                     f"{TaskPriority[it['pri']].label_ar}{pj}")
    lines.append("كل مسؤول هيوصله إشعار (وإيميل) بالتكليف.")
    return "\n".join(lines)


def _do_create(norm, company, user):
    from app.models import Task, TaskPriority, TaskStatus, Project
    from app.services.tasks_extras import set_assignees, log_activity
    from app.services.url_ids import encode_id
    created, warnings, failure = [], [], None
    for n, it in enumerate(norm, 1):
        try:
            t = Task(
                company_id=company.id, title=it["title"],
                description=it["desc"], project_id=it["project_id"],
                milestone_id=it["milestone_id"], assigned_to_id=it["ids"][0],
                created_by_id=user.id, priority=TaskPriority[it["pri"]],
                status=TaskStatus.TODO, deadline=it["dl"], notes=it["notes"])
            db.session.add(t)
            db.session.flush()
            tid = t.id
            set_assignees(t, it["ids"], actor_id=user.id)
            log_activity(t, "CREATED",
                         after={"title": it["title"], "ids": it["ids"],
                                "via": "agent"},
                         user_id=user.id)
            if it["project_id"]:
                p = db.session.get(Project, it["project_id"])
                if p:
                    p.recompute_progress()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("T-024 create item %s failed", n)
            failure = (n, str(e) if isinstance(e, ValueError) else "خطأ غير متوقع")
            break
        created.append({"title": it["title"], "url": "/tasks/" + encode_id(tid)})
        if not it["dl"]:
            warnings.append(f"«{it['title']}» اتسجّلت من غير تاريخ استحقاق")
    if failure and not created:
        return {"error": f"المهمة رقم {failure[0]} فشلت: {failure[1]}. "
                         "ما اتعملتش أي مهمة."}
    res = {"created": len(created), "links": created, "warnings": warnings,
           "message": f"اتعملت {len(created)} مهمة"
                      + (f" من {len(norm)}" if failure else "")}
    if failure:
        res["partial"] = True
        res["warnings"].append(
            f"المهمة رقم {failure[0]} فشلت ({failure[1]}) "
            "والمهام اللي بعدها ما اتعملتش.")
    return res


# ─── Update ─────────────────────────────────────────────────────────────
def _prepare_update(a, company, user):
    from app.models import TaskPriority, TaskStatus, Project, Milestone
    from app.services.tasks_extras import assignee_ids_for
    t = _load_task(a.get("task_id"), company, user)
    cid = company.id
    out = {"task_id": t.id}
    exp = {}
    lines = []
    if a.get("title") is not None:
        nt = str(a["title"]).strip()
        if not nt or len(nt) > 200:
            raise _WorkError("العنوان الجديد لازم يكون من 1 إلى 200 حرف")
        if nt != t.title:
            out["title"] = nt
            exp["title"] = t.title
            lines.append(f"- العنوان: «{t.title}» ← «{nt}»")
    if a.get("description") is not None:
        if t.created_by_id != user.id:
            raise _WorkError("وصف المهمة يتعدّل بس من اللي أنشأها")
        raw = str(a["description"]).strip()[:5000]
        if (_desc_html(raw) or None) != (t.description or None):
            out["description"] = raw
            exp["description"] = _sha(t.description)
            lines.append(f"- الوصف: «{_short(_plain(t.description))}» ← «{_short(raw)}»")
    if a.get("assignee_ids") is not None:
        ids, members = _assignees(cid, a["assignee_ids"])
        old = sorted(assignee_ids_for(t))
        if sorted(ids) != old:
            out["assignee_ids"] = ids
            exp["assignee_ids"] = old
            members = _members(cid)
            lines.append("- المسؤولين: " + "، ".join(_uname(members.get(i), i) for i in old)
                         + " ← " + "، ".join(_uname(members.get(i), i) for i in ids))
    if a.get("priority") is not None:
        p = str(a["priority"]).upper()
        if p not in _PRIORITIES:
            raise _WorkError("الأولوية لازم تكون LOW أو MEDIUM أو HIGH أو URGENT")
        if p != t.priority.value:
            out["priority"] = p
            exp["priority"] = t.priority.value
            lines.append(f"- الأولوية: {t.priority.label_ar} ← {TaskPriority[p].label_ar}")
    if a.get("status") is not None:
        s = str(a["status"]).upper()
        if s not in _STATUSES:
            raise _WorkError("الحالة غير معروفة")
        if s != t.status.value:
            out["status"] = s
            exp["status"] = t.status.value
            lines.append(f"- الحالة: {t.status.label_ar} ← {TaskStatus[s].label_ar}")
    dl_set, new_dl = False, None
    if a.get("clear_deadline") is True:
        dl_set = True
    elif not _empty(a.get("deadline")):
        new_dl = _pdate(a["deadline"], "تاريخ الاستحقاق")
        dl_set = True
    if dl_set and new_dl != t.deadline:
        if new_dl is None:
            out["clear_deadline"] = True
        else:
            out["deadline"] = new_dl.isoformat()
        exp["deadline"] = str(t.deadline) if t.deadline else None
        lines.append(f"- الاستحقاق: {t.deadline.isoformat() if t.deadline else 'بدون'}"
                     f" ← {new_dl.isoformat() if new_dl else 'بدون'}")
    if not _empty(a.get("project_id")) or not _empty(a.get("milestone_id")):
        proj_id = a["project_id"] if not _empty(a.get("project_id")) else t.project_id
        if not _empty(a.get("milestone_id")):
            mid_arg = a["milestone_id"]
        else:
            mid_arg = None if not _empty(a.get("project_id")) else t.milestone_id
        project, mid = _check_project(cid, proj_id, mid_arg)
        new_pid = project.id if project else None
        if new_pid != t.project_id:
            out["project_id"] = new_pid
            exp["project_id"] = t.project_id
            old_p = db.session.get(Project, t.project_id) if t.project_id else None
            lines.append(f"- المشروع: {old_p.name if old_p else 'بدون'} ← {project.name}")
        if mid != t.milestone_id:
            out["milestone_id"] = mid
            exp["milestone_id"] = t.milestone_id
            nm = db.session.get(Milestone, mid) if mid else None
            lines.append(f"- المرحلة: {nm.name if nm else 'بدون'}")
        if "project_id" in out and "milestone_id" not in out and t.milestone_id:
            # تغيير المشروع بيصفّر المرحلة القديمة
            out["milestone_id"] = None
            exp["milestone_id"] = t.milestone_id
            lines.append("- المرحلة: هتتمسح (المشروع اتغيّر)")
    if a.get("notes") is not None:
        nn = str(a["notes"]).strip()[:2000]
        if (nn or None) != (t.notes or None):
            out["notes"] = nn
            exp["notes"] = _sha(t.notes)
            lines.append("- الملاحظات: هتتغيّر")
    if len(out) == 1:
        raise _WorkError("مفيش تغيير فعلي في المهمة")
    out["_expect"] = exp
    summary = f"تعديل المهمة «{t.title}» (#{t.id}):\n" + "\n".join(lines)
    return out, summary


def _do_update(a, company, user):
    from app.models import TaskPriority, TaskStatus, Project, NotificationKind
    from app.services.tasks_extras import (
        log_activity, set_assignees, watchers_for, _notify, assignee_ids_for)
    from app.services.crm import set_task_status
    from app.services.url_ids import encode_id
    t = _load_task(a.get("task_id"), company, user)
    cur = {
        "title": t.title, "description": _sha(t.description),
        "assignee_ids": sorted(assignee_ids_for(t)),
        "priority": t.priority.value, "status": t.status.value,
        "deadline": str(t.deadline) if t.deadline else None,
        "project_id": t.project_id, "milestone_id": t.milestone_id,
        "notes": _sha(t.notes),
    }
    for k, v in (a.get("_expect") or {}).items():
        if cur.get(k) != v:
            raise _WorkError("المهمة اتغيّرت من وقت الاقتراح — اطلب التعديل من جديد")
    by = user.id
    old_pid = new_pid = t.project_id
    non_status = False
    if "title" in a:
        log_activity(t, "TITLE_CHANGED", before={"title": t.title},
                     after={"title": a["title"], "via": "agent"}, user_id=by)
        t.title = a["title"]
        non_status = True
    if "description" in a:
        if t.created_by_id != by:
            raise _WorkError("وصف المهمة يتعدّل بس من اللي أنشأها")
        nd = _desc_html(a["description"])
        log_activity(t, "DESCRIPTION_CHANGED",
                     before={"description": (t.description or "")[:200]},
                     after={"description": (nd or "")[:200], "via": "agent"},
                     user_id=by)
        t.description = nd
        non_status = True
    if "priority" in a:
        log_activity(t, "PRIORITY_CHANGED", before={"priority": t.priority.value},
                     after={"priority": a["priority"], "via": "agent"}, user_id=by)
        t.priority = TaskPriority[a["priority"]]
        non_status = True
    if "deadline" in a or a.get("clear_deadline"):
        nd = _pdate(a["deadline"], "تاريخ الاستحقاق") if "deadline" in a else None
        log_activity(t, "DEADLINE_CHANGED",
                     before={"deadline": str(t.deadline) if t.deadline else None},
                     after={"deadline": str(nd) if nd else None, "via": "agent"},
                     user_id=by)
        t.deadline = nd
        non_status = True
    if "project_id" in a or "milestone_id" in a:
        proj_id = a["project_id"] if "project_id" in a else t.project_id
        if "milestone_id" in a:
            mid_arg = a["milestone_id"]
        else:
            mid_arg = None if "project_id" in a else t.milestone_id
        project, mid = _check_project(company.id, proj_id, mid_arg)
        new_pid = project.id if project else None
        if new_pid != t.project_id:
            log_activity(t, "PROJECT_CHANGED", before={"project_id": t.project_id},
                         after={"project_id": new_pid, "via": "agent"}, user_id=by)
            t.project_id = new_pid
        if mid != t.milestone_id:
            log_activity(t, "MILESTONE_CHANGED", before={"milestone_id": t.milestone_id},
                         after={"milestone_id": mid, "via": "agent"}, user_id=by)
            t.milestone_id = mid
        non_status = True
    if "notes" in a:
        t.notes = (a["notes"] or "").strip() or None
        non_status = True
    db.session.flush()
    if "assignee_ids" in a:
        ids, _m = _assignees(company.id, a["assignee_ids"])
        set_assignees(t, ids, actor_id=by)
        non_status = True
    if new_pid != old_pid:
        for pid in (old_pid, new_pid):
            if pid:
                p = db.session.get(Project, pid)
                if p:
                    p.recompute_progress()
    db.session.commit()
    if "status" in a:
        ns = TaskStatus[a["status"]]
        if ns != t.status:
            log_activity(t, "STATUS_CHANGED", before={"status": t.status.value},
                         after={"status": ns.value, "via": "agent"}, user_id=by)
            set_task_status(t, ns, by_user_id=by)
    if non_status:
        try:
            for rid in watchers_for(t, exclude={by}):
                _notify(rid, company_id=t.company_id,
                        kind=NotificationKind.TASK_UPDATED,
                        title=f"✏️ تحديث على مهمة: {t.title}", body=None,
                        link_url="/tasks/" + encode_id(t.id), task=t)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("T-024 watcher notify failed")
    return {"updated": True, "message": f"اتعدّلت المهمة «{t.title}»",
            "links": [{"title": t.title, "url": "/tasks/" + encode_id(t.id)}],
            "warnings": []}


# ─── Propose / execute ──────────────────────────────────────────────────
def _propose(name, args, company, user):
    from app.models import AgentProposal, PROPOSAL_PENDING
    from app.services.agent_safety import _canonical_args
    from app.services.url_ids import encode_id
    if name == "create_tasks":
        clean = {"tasks": args.get("tasks")}
        summary = _summary_create(_normalize_create(clean, company))
    else:
        raw = {k: v for k, v in args.items()
               if k not in ("_confirmed_proposal_id", "_expect")}
        clean, summary = _prepare_update(raw, company, user)
    canonical = _canonical_args(clean)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    p = (AgentProposal.query
         .filter_by(company_id=company.id, user_id=user.id, tool_name=name,
                    status=PROPOSAL_PENDING)
         .filter(AgentProposal.created_at >= cutoff)
         .filter(AgentProposal.input_json == canonical)
         .order_by(AgentProposal.created_at.desc()).first())
    if p is None:
        p = AgentProposal(company_id=company.id, user_id=user.id,
                          tool_name=name, input_json=canonical,
                          summary_ar=summary, amount_readable="",
                          status=PROPOSAL_PENDING)
        db.session.add(p)
        db.session.commit()
    entry = {"proposal_id": encode_id(p.id), "tool": name,
             "summary_ar": p.summary_ar, "amount_readable": ""}
    lst = g.get("_t024_props")
    if lst is None:
        lst = []
        g._t024_props = lst
    lst.append(entry)
    res = dict(entry)
    res["requires_confirmation"] = True
    res["internal_note"] = ("اتجهّز اقتراح ومحتاج تأكيد المستخدم من البطاقة. "
                            "اطلب منه يراجع الملخص ويضغط «تأكيد ونفّذ». "
                            "ممنوع تقول إن المهمة اتسجّلت أو تم التعديل قبل التأكيد، "
                            "وممنوع تعد بأي ميعاد.")
    return res


def _execute_confirmed(name, args, company, user, confirmed):
    from app.models import AgentProposal, PROPOSAL_PENDING
    from app.services.agent_safety import _canonical_args
    if g.get("_t024_confirmed_pid") != confirmed:
        raise _WorkError("التأكيد غير صالح — لازم يتم من زرار «تأكيد ونفّذ»")
    p = db.session.get(AgentProposal, confirmed) if isinstance(confirmed, int) else None
    if (p is None or p.status != PROPOSAL_PENDING
            or p.company_id != company.id or p.user_id != user.id
            or p.tool_name != name or p.input_json != _canonical_args(args)):
        raise _WorkError("الاقتراح غير صالح للتنفيذ")
    if name == "create_tasks":
        return _do_create(_normalize_create({"tasks": args.get("tasks")}, company),
                          company, user)
    return _do_update(args, company, user)


def run_work_tool(name, args, company_id, user_id):
    args = args if isinstance(args, dict) else {}
    try:
        if name not in WORK_TOOL_NAMES:
            raise _WorkError("أداة غير معروفة")
        write = name in WORK_WRITE_TOOLS
        company, user = _ctx(company_id, user_id,
                             "tasks.manage" if write else "tasks.view")
        if name == "list_company_users":
            return _list_users(args, company)
        if name == "list_projects":
            return _list_projects(args, company)
        if name == "list_milestones":
            return _list_milestones(args, company)
        if name == "search_tasks":
            return _search_tasks(args, company, user)
        confirmed = args.get("_confirmed_proposal_id")
        if confirmed:
            return _execute_confirmed(name, args, company, user, confirmed)
        return _propose(name, args, company, user)
    except _WorkError as e:
        db.session.rollback()
        return {"error": str(e)}
    except Exception:
        db.session.rollback()
        current_app.logger.exception("work tool %s failed", name)
        return {"error": "حصلت مشكلة غير متوقعة، حاول مرة تانية."}
