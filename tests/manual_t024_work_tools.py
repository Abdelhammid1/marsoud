"""MARSOUD-P116 (T-024) — فحص أدوات المهام (create_tasks / update_task / search_tasks).

تحذير: بيكتب في قاعدة البيانات اللي في DATABASE_URL (شركتين ومستخدمين
مؤقتين بادئتهم __T024_) وبيمسحهم في الآخر. مش بيلمس أي شركة موجودة.
لازم تحمّل .env الأول (DATABASE_URL على PostgreSQL) وتشغّله بـ --yes.
مش بيشتغل من الـ deploy.
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402

PREFIX = "__T024_"
PLAN_CODE = "__t024__"
CHECKS = []
S = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _call(name, args, co, uid):
    from app.agent.work_tools import run_work_tool
    return run_work_tool(name, args, co, uid)


def _confirm(res, actor, co):
    from app.services.agent_safety import execute_proposal
    from app.services.url_ids import decode_id
    return execute_proposal(decode_id(res["proposal_id"]),
                            actor_user_id=actor, active_company_id=co)


def _cap(uid):
    from app.models import AgentDailyWriteCount
    row = AgentDailyWriteCount.query.filter_by(user_id=uid).first()
    return row.count if row else 0


def _task_count(co):
    from app.models import Task
    return Task.query.filter_by(company_id=co).count()


# ─── Setup / cleanup ────────────────────────────────────────────────────
def _purge(table, cond, path=()):
    """بيمسح صفوف table اللي تحقق cond وكل الصفوف اللي بتشاور عليها (FK)."""
    from sqlalchemy import select
    for t in list(db.metadata.sorted_tables):
        if t is table or t.name in path:
            continue
        for fk in t.foreign_keys:
            if fk.column.table is table:
                _purge(t, fk.parent.in_(select(fk.column).where(cond)),
                       path + (table.name,))
    db.session.execute(table.delete().where(cond))


def cleanup():
    from app.models import Company, Plan, User
    db.session.rollback()
    cos = [c.id for c in Company.query.filter(
        Company.name.startswith(PREFIX, autoescape=True)).all()
        if c.name.startswith(PREFIX)]
    users = [u.id for u in User.query.filter(
        User.email.startswith(PREFIX, autoescape=True)).all()
        if u.email.startswith(PREFIX)]
    try:
        if users:
            _purge(User.__table__, User.__table__.c.id.in_(users))
        if cos:
            _purge(Company.__table__, Company.__table__.c.id.in_(cos))
        plan = Plan.query.filter_by(code=PLAN_CODE).first()
        if plan is not None:
            if Company.query.filter_by(plan_id=plan.id).count() == 0:
                db.session.delete(plan)
        db.session.commit()
        print(f"cleanup: removed {len(cos)} companies, {len(users)} users")
    except Exception as e:
        db.session.rollback()
        print("!! cleanup فشل — الصفوف المؤقتة (__T024_) لسه موجودة:", e)


def setup():
    from decimal import Decimal
    from app.models import Company, Plan, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    cleanup()
    plan = Plan(code=PLAN_CODE, name="T024", name_ar="T024",
                allowed_subitems=None)
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports",
                      "agent", "settings"])
    db.session.add(plan)
    db.session.flush()

    def mk_co(sfx):
        co = Company(name=f"{PREFIX}CO_{sfx}__", base_currency="SAR",
                     vat_rate=Decimal("15"), plan_id=plan.id,
                     timezone="Asia/Riyadh")
        db.session.add(co)
        db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        return co.id

    def mk_user(co, tag, role):
        u = User(email=f"{PREFIX}{co}_{tag}@audit.local",
                 full_name=f"{tag}-{role}", is_active=True,
                 terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u)
        db.session.flush()
        set_membership_role(u.id, co, role)
        return u.id

    S["A"] = mk_co("A")
    S["B"] = mk_co("B")
    S["ownerA"] = mk_user(S["A"], "ownA", "owner")
    S["memA"] = mk_user(S["A"], "memA", "project_manager")
    S["ownerB"] = mk_user(S["B"], "ownB", "owner")
    db.session.commit()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("إنشاء بدون مسؤول: error ومفيش اقتراح")
def t1():
    from app.models import AgentProposal
    r = _call("create_tasks", {"tasks": [{"title": PREFIX + "x"}]},
              S["A"], S["ownerA"])
    assert "error" in r and "المسؤول" in r["error"], r
    assert AgentProposal.query.filter_by(company_id=S["A"]).count() == 0


@check("مسؤول من شركة تانية: مرفوض")
def t2():
    r = _call("create_tasks", {"tasks": [{"title": PREFIX + "x",
              "assignee_ids": [S["ownerB"]]}]}, S["A"], S["ownerA"])
    assert "error" in r, r


@check("اقتراح جماعي (2): بطاقة بـ hid ومفيش مهمة اتعملت")
def t3():
    args = {"tasks": [
        {"title": PREFIX + "T1", "assignee_ids": [S["memA"]],
         "deadline": "2030-01-15", "priority": "HIGH",
         "description": "سطر أول\nسطر تاني <script>alert(1)</script>"},
        {"title": PREFIX + "T2", "assignee_ids": [S["ownerA"]]},
    ]}
    r = _call("create_tasks", args, S["A"], S["ownerA"])
    assert r.get("requires_confirmation") is True, r
    assert len(r["proposal_id"]) == 8 and r["proposal_id"].isalnum(), r
    assert "بدون تاريخ استحقاق" in r["summary_ar"], r["summary_ar"]
    assert _task_count(S["A"]) == 0
    S["args"], S["res"] = args, r


@check("_confirmed_proposal_id مزوّر من الموديل: مرفوض ومفيش مهمة")
def t4():
    from app.services.url_ids import decode_id
    forged = dict(S["args"])
    forged["_confirmed_proposal_id"] = decode_id(S["res"]["proposal_id"])
    r = _call("create_tasks", forged, S["A"], S["ownerA"])
    assert "error" in r, r
    assert _task_count(S["A"]) == 0


@check("تأكيد من مستخدم تاني: 403 والاقتراح لسه PENDING")
def t5():
    from app.models import AgentProposal
    from app.services.url_ids import decode_id
    body, status = _confirm(S["res"], S["memA"], S["A"])
    assert status == 403, (status, body)
    assert _task_count(S["A"]) == 0
    p = db.session.get(AgentProposal, decode_id(S["res"]["proposal_id"]))
    assert p.status == "PENDING"


@check("تأكيد المنشئ: 2 مهمة + روابط + log بـ via + إشعار + وصف معقَّم")
def t6():
    from app.models import Task, TaskActivityLog, Notification
    body, status = _confirm(S["res"], S["ownerA"], S["A"])
    assert status == 200 and body.get("ok"), (status, body)
    res = body["result"]
    assert res["created"] == 2 and len(res["links"]) == 2, res
    assert all(l["url"].startswith("/tasks/") for l in res["links"]), res
    assert any("من غير تاريخ" in w for w in res["warnings"]), res
    ts = Task.query.filter_by(company_id=S["A"]).order_by(Task.id).all()
    assert [t.title for t in ts] == [PREFIX + "T1", PREFIX + "T2"], ts
    t1_, t2_ = ts
    assert t1_.description and "<script" not in t1_.description.lower(), t1_.description
    assert t1_.created_by_id == S["ownerA"] and t1_.assigned_to_id == S["memA"]
    assert str(t1_.deadline) == "2030-01-15" and t1_.priority.value == "HIGH"
    logs = TaskActivityLog.query.filter_by(task_id=t1_.id, action="CREATED").all()
    assert logs and '"via": "agent"' in (logs[0].after_json or ""), logs
    n = Notification.query.filter_by(user_id=S["memA"], company_id=S["A"]).count()
    assert n >= 1, "مفيش إشعار تكليف"
    S["t1"], S["t2"] = t1_.id, t2_.id


@check("تأكيد تاني على نفس الاقتراح: 400 ومفيش تكرار")
def t7():
    body, status = _confirm(S["res"], S["ownerA"], S["A"])
    assert status == 400, (status, body)
    assert _task_count(S["A"]) == 2


@check("تعديل (عنوان + استحقاق): اقتراح بالقديم والجديد ثم تأكيد + log")
def t8():
    from app.models import Task, TaskActivityLog
    r = _call("update_task", {"task_id": S["t2"], "title": PREFIX + "T2-new",
                              "deadline": "2030-02-01"}, S["A"], S["ownerA"])
    assert r.get("requires_confirmation") is True, r
    assert "←" in r["summary_ar"], r["summary_ar"]
    body, status = _confirm(r, S["ownerA"], S["A"])
    assert status == 200 and body.get("ok"), (status, body)
    t = db.session.get(Task, S["t2"])
    assert t.title == PREFIX + "T2-new" and str(t.deadline) == "2030-02-01"
    acts = {a.action for a in TaskActivityLog.query.filter_by(task_id=S["t2"]).all()}
    assert {"TITLE_CHANGED", "DEADLINE_CHANGED"} <= acts, acts


@check("تعديل اتغيّر في النص: 400 والاقتراح PENDING والعداد اليومي ما زادش")
def t9():
    from app.models import Task, AgentProposal
    from app.services.url_ids import decode_id
    r = _call("update_task", {"task_id": S["t1"], "priority": "URGENT"},
              S["A"], S["ownerA"])
    assert r.get("requires_confirmation") is True, r
    t = db.session.get(Task, S["t1"])
    from app.models import TaskPriority
    t.priority = TaskPriority.LOW
    db.session.commit()
    before = _cap(S["ownerA"])
    body, status = _confirm(r, S["ownerA"], S["A"])
    assert status == 400 and "error" in body, (status, body)
    assert _cap(S["ownerA"]) == before, (before, _cap(S["ownerA"]))
    p = db.session.get(AgentProposal, decode_id(r["proposal_id"]))
    assert p.status == "PENDING"


@check("تعديل مهمة من شركة تانية: مرفوض")
def t10():
    from app.models import Task, TaskPriority, TaskStatus
    tb = Task(company_id=S["B"], title=PREFIX + "B1",
              assigned_to_id=S["ownerB"], created_by_id=S["ownerB"],
              priority=TaskPriority.MEDIUM, status=TaskStatus.TODO)
    db.session.add(tb)
    db.session.commit()
    r = _call("update_task", {"task_id": tb.id, "title": "x"},
              S["A"], S["ownerA"])
    assert "error" in r and not r.get("requires_confirmation"), r


@check("وصف المهمة يتعدّل بس من المنشئ")
def t11():
    r = _call("update_task", {"task_id": S["t1"], "description": "جديد"},
              S["A"], S["memA"])
    assert "error" in r and "أنشأها" in r["error"], r


@check("search_tasks: عزل الشركات + رؤية المستخدم العادي")
def t12():
    from app.models import Task
    rb = _call("search_tasks", {"query": PREFIX}, S["B"], S["ownerB"])
    assert [t["title"] for t in rb["tasks"]] == [PREFIX + "B1"], rb
    ra = _call("search_tasks", {"query": PREFIX}, S["A"], S["ownerA"])
    assert ra["count"] == 2 and all(t["link"].startswith("/tasks/") for t in ra["tasks"]), ra
    rm = _call("search_tasks", {"query": PREFIX}, S["A"], S["memA"])
    ids = {t["id"] for t in rm["tasks"]}
    own = {t.id for t in Task.query.filter_by(company_id=S["A"]).all()
           if S["memA"] in (t.assigned_to_id, t.created_by_id)}
    from app.agent.work_tools import _visibility
    from app.models import Company, User
    full, _pm = _visibility(db.session.get(Company, S["A"]),
                            db.session.get(User, S["memA"]))
    if full:
        print("   (ملاحظة: الدور project_manager عنده رؤية كاملة هنا، تخطّيت شرط الرؤية الضيقة)")
    else:
        assert ids <= own and S["t1"] in ids and S["t2"] not in ids, (ids, own)


@check("list_company_users: مستخدمي الشركة بس")
def t13():
    r = _call("list_company_users", {}, S["A"], S["ownerA"])
    ids = {u["id"] for u in r["users"]}
    assert S["ownerA"] in ids and S["memA"] in ids and S["ownerB"] not in ids, r


def main():
    app = create_app()
    with app.app_context():
        url = db.engine.url
        print("DB:", url.render_as_string(hide_password=True))
        if url.get_backend_name() != "postgresql":
            print("STOP: DATABASE_URL مش على PostgreSQL (حمّل .env الأول).")
            return 2
        if "--yes" not in sys.argv:
            print("STOP: ده بيكتب شركتين مؤقتتين في القاعدة اللي فوق. شغّله بـ --yes.")
            return 2
        import app.services.email as _em
        _em.send_task_notification_email = lambda *a, **k: None
        fails = 0
        try:
            setup()
            for label, fn in CHECKS:
                try:
                    fn()
                    print("PASS", label)
                except Exception as e:
                    db.session.rollback()
                    fails += 1
                    print("FAIL", label, "->", repr(e)[:400])
        finally:
            cleanup()
        print(f"\n{len(CHECKS) - fails}/{len(CHECKS)} passed")
        return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
