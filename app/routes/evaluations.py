"""MARSOUD-EVALUATIONS — routes for the monthly-evaluation workflow.

Mounted at /evaluations. Owner + admin only for now (permission gate:
users.manage). Sub-flows:

  · GET  /                       list every cycle in the company
  · POST /new                    create a cycle
  · GET  /<cid>                  cycle detail — one card per employee
                                 with targets/actuals/score
  · POST /<cid>/delete           delete a cycle (OPEN only)
  · POST /<cid>/status           transition_status (OPEN→SUBMITTED→LOCKED)
  · POST /<cid>/reopen           SUBMITTED/LOCKED → OPEN (owner)
  · POST /<cid>/targets          upsert a target
  · POST /<cid>/targets/<tid>/delete
  · POST /<cid>/actuals          upsert an actual
  · POST /<cid>/actuals/<aid>/delete
  · POST /<cid>/employees/<eid>/compute   recompute score
  · POST /<cid>/employees/<eid>/review    save reviewer notes
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    EvaluationCycle, EvaluationCyclePeriod, EvaluationCycleStatus,
    EvaluationCategory, ActualSource,
    EmployeeTarget, EmployeeMetricActual, EmployeeEvaluation,
    Employee, EmployeeStatus,
)
from app.services.permissions import require_permission
from app.services.evaluation import (
    create_cycle, delete_cycle, transition_status,
    upsert_target, delete_target,
    upsert_actual, delete_actual,
    compute_score, update_review,
    EvaluationError,
)


bp = Blueprint("evaluations", __name__)


def _cycle_or_404(cid):
    c = db.session.get(EvaluationCycle, cid)
    if not c or c.company_id != g.active_company.id:
        abort(404)
    return c


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ─── List + create ─────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("users.manage")
def index():
    cid = g.active_company.id
    cycles = (EvaluationCycle.query
                .filter_by(company_id=cid)
                .order_by(EvaluationCycle.start_date.desc())
                .all())
    return render_template(
        "evaluations/index.html",
        cycles=cycles,
        periods=EvaluationCyclePeriod,
        statuses=EvaluationCycleStatus,
    )


@bp.route("/new", methods=["POST"])
@login_required
@require_permission("users.manage")
def new():
    try:
        c = create_cycle(
            company_id=g.active_company.id,
            name=request.form.get("name", ""),
            period_type=request.form.get("period_type", "MONTHLY"),
            start_date=_parse_date(request.form.get("start_date")),
            end_date=_parse_date(request.form.get("end_date")),
            created_by_id=current_user.id,
        )
        flash(f"تم إنشاء دورة التقييم: {c.name}", "success")
        return redirect(url_for("evaluations.detail", cid=c.id))
    except EvaluationError as e:
        flash(str(e), "error")
        return redirect(url_for("evaluations.index"))


@bp.route("/<int:cid>/delete", methods=["POST"])
@login_required
@require_permission("users.manage")
def delete(cid):
    c = _cycle_or_404(cid)
    try:
        delete_cycle(c)
        flash("تم حذف الدورة", "success")
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.index"))


# ─── Cycle detail ──────────────────────────────────────────────────────
@bp.route("/<int:cid>")
@login_required
@require_permission("users.manage")
def detail(cid):
    c = _cycle_or_404(cid)
    company_id = g.active_company.id
    # Only ACTIVE employees can have new targets added — but historical
    # cycles may reference terminated employees, so we still show them
    # when they have data in this cycle.
    active_employees = (Employee.query
                          .filter_by(company_id=company_id,
                                       status=EmployeeStatus.ACTIVE)
                          .order_by(Employee.name)
                          .all())
    # Preload targets, actuals, evaluations keyed by employee
    targets = EmployeeTarget.query.filter_by(cycle_id=c.id).all()
    actuals = EmployeeMetricActual.query.filter_by(cycle_id=c.id).all()
    evaluations = EmployeeEvaluation.query.filter_by(cycle_id=c.id).all()

    by_emp = {}
    for e in active_employees:
        by_emp[e.id] = {"employee": e, "targets": [], "actuals": [],
                          "evaluation": None}
    for t in targets:
        bucket = by_emp.setdefault(
            t.employee_id,
            {"employee": t.employee, "targets": [], "actuals": [],
              "evaluation": None},
        )
        bucket["targets"].append(t)
    for a in actuals:
        bucket = by_emp.setdefault(
            a.employee_id,
            {"employee": a.employee, "targets": [], "actuals": [],
              "evaluation": None},
        )
        bucket["actuals"].append(a)
    for ev in evaluations:
        bucket = by_emp.setdefault(
            ev.employee_id,
            {"employee": ev.employee, "targets": [], "actuals": [],
              "evaluation": None},
        )
        bucket["evaluation"] = ev

    return render_template(
        "evaluations/detail.html",
        cycle=c,
        buckets=list(by_emp.values()),
        categories=EvaluationCategory,
        sources=ActualSource,
    )


# ─── State transitions ────────────────────────────────────────────────
@bp.route("/<int:cid>/status", methods=["POST"])
@login_required
@require_permission("users.manage")
def status(cid):
    c = _cycle_or_404(cid)
    try:
        new = request.form.get("new_status", "").strip()
        transition_status(c, new)
        flash(f"تم تحديث الحالة إلى {c.status_label_ar}", "success")
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


# ─── Target CRUD ──────────────────────────────────────────────────────
@bp.route("/<int:cid>/targets", methods=["POST"])
@login_required
@require_permission("users.manage")
def target_upsert(cid):
    c = _cycle_or_404(cid)
    try:
        emp_id = int(request.form.get("employee_id"))
        upsert_target(
            cycle=c, employee_id=emp_id,
            metric_key=request.form.get("metric_key", ""),
            target_value=request.form.get("target_value", "0"),
            weight_pct=request.form.get("weight_pct", "0"),
            category=request.form.get("category", ""),
            notes=request.form.get("notes"),
        )
        flash("تم حفظ الهدف", "success")
    except (EvaluationError, ValueError, TypeError) as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


@bp.route("/<int:cid>/targets/<int:tid>/delete", methods=["POST"])
@login_required
@require_permission("users.manage")
def target_delete(cid, tid):
    c = _cycle_or_404(cid)
    t = db.session.get(EmployeeTarget, tid)
    if not t or t.cycle_id != c.id:
        abort(404)
    try:
        delete_target(t)
        flash("تم حذف الهدف", "success")
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


# ─── Actual CRUD ──────────────────────────────────────────────────────
@bp.route("/<int:cid>/actuals", methods=["POST"])
@login_required
@require_permission("users.manage")
def actual_upsert(cid):
    c = _cycle_or_404(cid)
    try:
        emp_id = int(request.form.get("employee_id"))
        upsert_actual(
            cycle=c, employee_id=emp_id,
            metric_key=request.form.get("metric_key", ""),
            actual_value=request.form.get("actual_value", "0"),
            source=request.form.get("source", "MANUAL"),
            entered_by_id=current_user.id,
            evidence_note=request.form.get("evidence_note"),
        )
        flash("تم حفظ الرقم الفعلي", "success")
    except (EvaluationError, ValueError, TypeError) as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


@bp.route("/<int:cid>/actuals/<int:aid>/delete", methods=["POST"])
@login_required
@require_permission("users.manage")
def actual_delete(cid, aid):
    c = _cycle_or_404(cid)
    a = db.session.get(EmployeeMetricActual, aid)
    if not a or a.cycle_id != c.id:
        abort(404)
    try:
        delete_actual(a)
        flash("تم حذف الرقم الفعلي", "success")
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


# ─── Compute + review ────────────────────────────────────────────────
@bp.route("/<int:cid>/employees/<int:eid>/compute", methods=["POST"])
@login_required
@require_permission("users.manage")
def score(cid, eid):
    c = _cycle_or_404(cid)
    emp = db.session.get(Employee, eid)
    if not emp or emp.company_id != g.active_company.id:
        abort(404)
    try:
        ev = compute_score(c, eid)
        flash(
            f"تم حساب سكور {emp.name}: {float(ev.final_score):.1f}/100 "
            f"({ev.bonus_tier_label_ar})",
            "success",
        )
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))


@bp.route("/<int:cid>/employees/<int:eid>/review", methods=["POST"])
@login_required
@require_permission("users.manage")
def review(cid, eid):
    c = _cycle_or_404(cid)
    ev = EmployeeEvaluation.query.filter_by(
        cycle_id=c.id, employee_id=eid,
    ).first()
    if not ev:
        flash("لا يوجد تقييم محسوب لهذا الموظف بعد. احسب السكور أولاً.",
               "error")
        return redirect(url_for("evaluations.detail", cid=c.id))
    try:
        update_review(ev, request.form.get("review_notes", ""),
                       by_user_id=current_user.id)
        flash("تم حفظ الملاحظات", "success")
    except EvaluationError as e:
        flash(str(e), "error")
    return redirect(url_for("evaluations.detail", cid=c.id))
