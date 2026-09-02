"""MARSOUD-TKT-HR-DECISIONS-01 (2026-09-02) — قرارات الموظفين routes.

Blueprint `hr_decisions` mounted at `/hr/decisions`. Six endpoints
covering the create / execute / cancel / list / detail flow. Every
mutating endpoint routes through the service in
`app/services/hr_decisions.py` so audit logging + the immutable-once-
executed guard are enforced in ONE place.

Permission gates map to existing ones (no new permission this ticket):
  * List / detail          → payroll.view
  * Create + execute of
    ADMIN + TERMINATION    → payroll.employees
  * Create + execute of
    FINANCIAL              → payroll.accruals  (posts a JE)
  * Cancel                 → same permission as create for that kind
"""
from flask import (
    Blueprint, render_template, redirect, url_for, request, g, flash,
    abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    HrDecision, HrDecisionKind, HrDecisionStatus,
    Employee, hr_decision_category,
)
from app.services.permissions import require_permission, has_permission
from app.services.ledger import cash_and_bank_accounts
from app.services.hr_decisions import (
    HrDecisionError, create_decision, execute_decision,
    cancel_decision, list_decisions,
)


bp = Blueprint("hr_decisions", __name__)


# ─── Permission helper ──────────────────────────────────────────
def _write_perm_for_kind(kind_str):
    """Which permission the current user needs to create / execute this
    kind. FINANCIAL is JE-posting → payroll.accruals; everything else
    is lifecycle → payroll.employees."""
    try:
        cat = hr_decision_category(HrDecisionKind(kind_str))
    except (ValueError, TypeError):
        cat = "ADMIN"
    return ("payroll.accruals" if cat == "FINANCIAL"
            else "payroll.employees")


def _load_decision_or_404(dec_id):
    dec = db.session.get(HrDecision, int(dec_id))
    if not dec or dec.company_id != g.active_company.id:
        abort(404)
    return dec


# ─── Index ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("payroll.view")
def index():
    cid = g.active_company.id
    status = (request.args.get("status") or "").strip() or None
    kind = (request.args.get("kind") or "").strip() or None
    emp_id = request.args.get("employee_id", type=int)
    rows = list_decisions(cid, status=status, kind=kind,
                           employee_id=emp_id)
    return render_template(
        "hr_decisions/index.html",
        rows=rows,
        employees=(Employee.query
                    .filter_by(company_id=cid)
                    .order_by(Employee.name).all()),
        filters={"status": status, "kind": kind, "employee_id": emp_id},
        kinds=HrDecisionKind,
        statuses=HrDecisionStatus,
    )


# ─── New form ───────────────────────────────────────────────────
@bp.route("/new")
@login_required
def new():
    cid = g.active_company.id
    kind = (request.args.get("kind") or "APPOINTMENT").strip()
    emp_id = request.args.get("employee_id", type=int)
    perm = _write_perm_for_kind(kind)
    if not has_permission(perm):
        flash("ليس لديك صلاحية لهذا النوع من القرارات", "error")
        return redirect(url_for("hr_decisions.index"))
    return render_template(
        "hr_decisions/form.html",
        kind=kind,
        selected_employee_id=emp_id,
        employees=(Employee.query
                    .filter_by(company_id=cid)
                    .order_by(Employee.name).all()),
        payment_groups=cash_and_bank_accounts(cid),
        kinds=HrDecisionKind,
        category=(hr_decision_category(HrDecisionKind(kind))
                    if kind in {k.value for k in HrDecisionKind}
                    else "ADMIN"),
    )


# ─── Create ─────────────────────────────────────────────────────
@bp.route("/create", methods=["POST"])
@login_required
def create():
    cid = g.active_company.id
    kind = (request.form.get("kind") or "").strip()
    perm = _write_perm_for_kind(kind)
    if not has_permission(perm):
        flash("ليس لديك صلاحية لإنشاء هذا النوع من القرارات", "error")
        return redirect(url_for("hr_decisions.index"))
    try:
        dec = create_decision(
            cid,
            employee_id=request.form.get("employee_id", type=int),
            kind=kind,
            effective_date=(request.form.get("effective_date") or "").strip(),
            title=request.form.get("title"),
            body=request.form.get("body"),
            reference=request.form.get("reference"),
            timing=(request.form.get("timing") or "IMMEDIATE").strip(),
            amount=request.form.get("amount"),
            payment_account_id=request.form.get("payment_account_id",
                                                 type=int),
            actor_id=current_user.id,
        )
        flash("تم حفظ القرار كمسودة", "success")
        return redirect(url_for("hr_decisions.detail", dec_id=dec.id))
    except HrDecisionError as e:
        flash(str(e), "error")
        return redirect(url_for("hr_decisions.new",
                                 kind=kind,
                                 employee_id=request.form.get("employee_id")))


# ─── Detail ─────────────────────────────────────────────────────
@bp.route("/<int:dec_id>")
@login_required
@require_permission("payroll.view")
def detail(dec_id):
    dec = _load_decision_or_404(dec_id)
    return render_template("hr_decisions/detail.html", dec=dec)


# ─── Execute ────────────────────────────────────────────────────
@bp.route("/<int:dec_id>/execute", methods=["POST"])
@login_required
def execute(dec_id):
    dec = _load_decision_or_404(dec_id)
    perm = _write_perm_for_kind(dec.kind)
    if not has_permission(perm):
        flash("ليس لديك صلاحية لتنفيذ هذا النوع", "error")
        return redirect(url_for("hr_decisions.detail", dec_id=dec.id))
    try:
        execute_decision(dec, actor_id=current_user.id)
        flash("تم تنفيذ القرار", "success")
    except HrDecisionError as e:
        flash(str(e), "error")
    except Exception as e:  # noqa: BLE001
        flash(str(e) or "فشل تنفيذ القرار", "error")
    return redirect(url_for("hr_decisions.detail", dec_id=dec.id))


# ─── Cancel ─────────────────────────────────────────────────────
@bp.route("/<int:dec_id>/cancel", methods=["POST"])
@login_required
def cancel(dec_id):
    dec = _load_decision_or_404(dec_id)
    perm = _write_perm_for_kind(dec.kind)
    if not has_permission(perm):
        flash("ليس لديك صلاحية للإلغاء", "error")
        return redirect(url_for("hr_decisions.detail", dec_id=dec.id))
    reason = (request.form.get("reason") or "").strip()
    try:
        cancel_decision(dec, reason=reason, actor_id=current_user.id)
        flash("تم إلغاء القرار", "success")
    except HrDecisionError as e:
        flash(str(e), "error")
    return redirect(url_for("hr_decisions.detail", dec_id=dec.id))
