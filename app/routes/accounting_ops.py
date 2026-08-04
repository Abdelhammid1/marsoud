"""MARSOUD-ACCOUNTING-OPS — 🧮 العمليات المحاسبية.

Two routes, both driven entirely by the OPERATIONS registry in
app/services/accounting_ops.py:

    GET  /accounting-ops/          the cards
    GET  /accounting-ops/<op_key>  the wizard form
    POST /accounting-ops/<op_key>  post the journal

Adding an operation touches the registry only — never this file, never the
templates, never the sidebar. That is the point of the ticket.

Gated on `journals.create`: these wizards automate exactly what that
permission already allows (posting a journal), so they inherit its role
list rather than inventing a parallel one.
"""
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
)
from flask_login import login_required, current_user

from app.services.accounting_ops import (
    OPERATIONS, get_operation, run_operation, OperationError,
    field_choices, SELECT_KINDS, EMPTY_PICKER_MESSAGES,
)
from app.services.permissions import require_permission


bp = Blueprint("accounting_ops", __name__)


@bp.route("/")
@login_required
@require_permission("journals.create")
def index():
    return render_template("accounting_ops/index.html", operations=OPERATIONS)


@bp.route("/<op_key>", methods=["GET", "POST"])
@login_required
@require_permission("journals.create")
def run(op_key):
    op = get_operation(op_key)
    if not op:
        flash("عملية غير معروفة", "error")
        return redirect(url_for("accounting_ops.index"))

    if request.method == "POST":
        try:
            entry = run_operation(
                op, g.active_company.id, request.form,
                actor_id=current_user.id,
            )
            flash(f"تم تنفيذ «{op.title}» — القيد {entry.number}", "success")
            return redirect(url_for("journals.view", entry_id=entry.id))
        except OperationError as e:
            flash(str(e), "error")

    # MARSOUD-OPS-FOUNDATION (2026-08-05) — one call builds every picker
    # on the form, whatever kinds it uses. The route no longer knows that
    # cash accounts exist; adding a picker kind touches the registry and
    # field_choices, never this file.
    return render_template(
        "accounting_ops/run.html", op=op,
        choices=field_choices(op, g.active_company.id),
        select_kinds=SELECT_KINDS,
        empty_messages=EMPTY_PICKER_MESSAGES,
    )
