"""MARSOUD-PLANS-COMPLETE (Abdelhamid 2026-07-22) — owner-facing
usage dashboard at /settings/usage.

Renders per-quota consumption bars, month-to-date overage, and a
per-employee AI token breakdown. Owner-only (matches the pattern
of settings_backup.py)."""
from flask import Blueprint, render_template, redirect, url_for, flash, g
from flask_login import login_required, current_user

from app import db
from app.models import (
    QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES,
    QUOTA_BRANCHES, EmployeeAiCap, User,
)
from app.models.user import user_companies
from app.services.quotas import (
    get_quota, count_current, count_ai_tokens_this_month,
    compute_overage,
)

bp = Blueprint("settings_usage", __name__)


QUOTA_LABELS = {
    QUOTA_USERS: "المستخدمون",
    QUOTA_AI_TOKENS_MONTH: "توكنز الوكيل الذكي (شهرياً)",
    QUOTA_STORAGE_BYTES: "المساحة التخزينية",
    QUOTA_BRANCHES: "الفروع",
}


def _is_owner():
    if not g.get("active_company"):
        return False
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == current_user.id) &
            (user_companies.c.company_id == g.active_company.id) &
            (user_companies.c.role == "owner"),
        )
    ).first()
    return row is not None


def _bar_color(pct):
    if pct >= 90:
        return "red"
    if pct >= 70:
        return "yellow"
    return "green"


def _humanize_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@bp.route("/", methods=["GET"])
@login_required
def index():
    if not _is_owner():
        flash("هذه الصفحة مخصّصة لصاحب الشركة فقط.", "error")
        return redirect(url_for("dashboard.index"))
    company = g.active_company
    cards = []
    for qt in (QUOTA_USERS, QUOTA_AI_TOKENS_MONTH,
                QUOTA_STORAGE_BYTES, QUOTA_BRANCHES):
        q = get_quota(company, qt)
        if q is None:
            continue
        used = count_current(company, qt)
        limit = int(q.included_amount or 0) or 1
        pct = min(100, int((used / limit) * 100)) if limit else 0
        display_used = display_limit = None
        if qt == QUOTA_STORAGE_BYTES:
            display_used = _humanize_bytes(used)
            display_limit = _humanize_bytes(q.included_amount)
        cards.append({
            "quota_type": qt,
            "label": QUOTA_LABELS.get(qt, qt),
            "used": used,
            "limit": int(q.included_amount or 0),
            "pct": pct,
            "color": _bar_color(pct),
            "enforcement": q.enforcement_mode,
            "display_used": display_used,
            "display_limit": display_limit,
        })

    # Per-employee AI usage — only the users belonging to THIS company.
    team_ids = [r.user_id for r in db.session.execute(
        user_companies.select().where(
            user_companies.c.company_id == company.id)
    ).fetchall()]
    per_employee = []
    for uid in team_ids:
        u = db.session.get(User, uid)
        if not u:
            continue
        used = count_ai_tokens_this_month(company, user_id=uid)
        cap = EmployeeAiCap.query.filter_by(
            company_id=company.id, user_id=uid).first()
        per_employee.append({
            "user": u,
            "used": used,
            "cap": cap.monthly_cap if cap else None,
        })
    per_employee.sort(key=lambda r: -r["used"])

    overage = compute_overage(company)
    overage_rows = [
        {"quota_type": qt, "label": QUOTA_LABELS.get(qt, qt), **row}
        for qt, row in overage.items()
    ]
    overage_total = sum(r["amount"] for r in overage_rows)

    return render_template(
        "settings/usage.html",
        cards=cards, per_employee=per_employee,
        overage_rows=overage_rows, overage_total=overage_total,
    )


@bp.route("/set-cap/<int:user_id>", methods=["POST"])
@login_required
def set_cap(user_id):
    if not _is_owner():
        flash("صلاحية المالك فقط.", "error")
        return redirect(url_for("settings_usage.index"))
    from flask import request
    company = g.active_company
    raw = (request.form.get("monthly_cap") or "").strip()
    if not raw:
        # Blank ⇒ remove the cap.
        EmployeeAiCap.query.filter_by(
            company_id=company.id, user_id=user_id).delete()
        db.session.commit()
        flash("تم إلغاء الحد الشهري لهذا الموظف.", "success")
        return redirect(url_for("settings_usage.index"))
    try:
        val = int(raw)
        if val <= 0:
            raise ValueError
    except ValueError:
        flash("قيمة الحد يجب أن تكون عدد صحيح موجب.", "error")
        return redirect(url_for("settings_usage.index"))
    cap = EmployeeAiCap.query.filter_by(
        company_id=company.id, user_id=user_id).first()
    if cap is None:
        cap = EmployeeAiCap(company_id=company.id, user_id=user_id)
        db.session.add(cap)
    cap.monthly_cap = val
    db.session.commit()
    flash("تم حفظ الحد الشهري.", "success")
    return redirect(url_for("settings_usage.index"))
