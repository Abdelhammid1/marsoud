from flask import (
    Blueprint, render_template, redirect, url_for, g,
    request, send_from_directory, current_app,
)
from flask_login import login_required, current_user
from app.services.reports import dashboard_metrics
from app.services.invoicing import update_overdue_statuses

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def landing():
    return send_from_directory(current_app.static_folder, "landing.html")


@bp.route("/home")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    update_overdue_statuses(g.active_company.id)
    # MARSOUD-DASH-FILTER — اليوم / الشهر / الربع / السنة
    period = request.args.get("period", "month")
    metrics = dashboard_metrics(g.active_company.id, period=period)
    # MARSOUD-TASK-ARCHIVE-MINE (2026-08-08) — the "🗂 أرشيفي"
    # dashboard tile is per-user, so it lives outside the
    # company-scoped dashboard_metrics(). Computed here so the
    # metrics dict remains a single source of truth for the tile
    # renderer.
    try:
        from app.services.task_archive import my_archived_tasks
        metrics.setdefault("ops", {})["tasks_archived_mine"] = (
            my_archived_tasks(
                g.active_company.id, current_user.id).count())
    except Exception:
        current_app.logger.exception("tasks_archived_mine metric failed")
        metrics.setdefault("ops", {})["tasks_archived_mine"] = 0
    # MARSOUD-TKT-TREASURY-HUB-01 — dashboard tile shows combined
    # cash + bank balance. Wrapped so a treasury-side error doesn't
    # kill the whole dashboard render.
    try:
        from app.services.treasury import kpi as treasury_kpi
        stats = treasury_kpi(g.active_company.id)
        metrics.setdefault("ops", {})["treasury_combined"] = stats["combined"]
        metrics.setdefault("ops", {})["treasury_account_count"] = stats["account_count"]
    except Exception:
        current_app.logger.exception("treasury metric failed")
        metrics.setdefault("ops", {})["treasury_combined"] = 0.0
        metrics.setdefault("ops", {})["treasury_account_count"] = 0
    # MARSOUD-TKT-HR-DECISIONS-01 — dashboard tile for pending HR
    # decisions. Same wrap-in-try guard.
    try:
        from app.models import HrDecision
        pending = (HrDecision.query
                    .filter_by(company_id=g.active_company.id)
                    .filter(HrDecision.status.in_(
                        ["DRAFT", "PENDING_PAYROLL"]))
                    .count())
        metrics.setdefault("ops", {})["hr_decisions_pending"] = pending
    except Exception:
        current_app.logger.exception("hr_decisions metric failed")
        metrics.setdefault("ops", {})["hr_decisions_pending"] = 0
    # MARSOUD-HR-EMPLOYEE-DOCS-01 — dashboard tile for employees with
    # any missing / expired mandatory paper. Same wrap; 0 for a tenant
    # that hasn't defined any required document types yet.
    try:
        from app.services.employee_documents import (
            count_employees_with_missing_docs,
        )
        metrics.setdefault("ops", {})["employees_missing_docs"] = (
            count_employees_with_missing_docs(g.active_company.id))
    except Exception:
        current_app.logger.exception(
            "employees_missing_docs metric failed")
        metrics.setdefault("ops", {})["employees_missing_docs"] = 0
    # MARSOUD-PURCHASE-ORDERS-01 — pending PO count (REQUESTED +
    # APPROVED + PARTIALLY_RECEIVED). Same wrap.
    try:
        from app.models import PurchaseOrder, PurchaseOrderStatus
        po_pending = (PurchaseOrder.query
                       .filter_by(company_id=g.active_company.id)
                       .filter(PurchaseOrder.deleted_at.is_(None))
                       .filter(PurchaseOrder.status.in_((
                           PurchaseOrderStatus.REQUESTED,
                           PurchaseOrderStatus.APPROVED,
                           PurchaseOrderStatus.PARTIALLY_RECEIVED)))
                       .count())
        metrics.setdefault("ops", {})["purchase_orders_pending"] = po_pending
    except Exception:
        current_app.logger.exception("purchase_orders metric failed")
        metrics.setdefault("ops", {})["purchase_orders_pending"] = 0
    return render_template("dashboard/index.html", metrics=metrics)
