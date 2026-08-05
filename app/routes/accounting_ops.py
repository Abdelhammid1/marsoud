"""MARSOUD-ACCOUNTING-OPS — 🧮 العمليات المحاسبية.

Two routes, both driven entirely by the OPERATIONS registry in
app/services/accounting_ops.py:

    GET  /accounting-ops/          the cards, grouped
    GET  /accounting-ops/<op_key>  the wizard form
    POST /accounting-ops/<op_key>  post the journal

Adding an operation touches the registry only — never this file, never the
templates, never the sidebar. That is the point of the ticket.

PERMISSIONS (MARSOUD-OPS-FOUNDATION §6, 2026-08-05)
===================================================
Every wizard used to sit behind one gate, `journals.create`. That made
"let the cashier move money from the till to the bank" indistinguishable
from "let the cashier inject capital and record owner drawings".

Each operation now declares its own `permission`, checked INSIDE `run()`
rather than by a decorator — a static decorator cannot vary per op_key.
Filtering the index is presentation; the check in `run` is the protection,
because hiding a card does nothing about someone opening the URL.

The page itself stays on `journals.create`: it is the ledger-writing area,
and the per-operation codes are all implied by it (see `_IMPLIES` in
services/permissions.py), so no existing role loses anything.
"""
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
)
from flask_login import login_required, current_user

from app.services.accounting_ops import (
    OPERATIONS, GROUPS, get_operation, run_operation, OperationError,
    field_choices, SELECT_KINDS, EMPTY_PICKER_MESSAGES,
)
from app.services.permissions import require_permission, has_permission


bp = Blueprint("accounting_ops", __name__)


@bp.route("/")
@login_required
@require_permission("journals.create")
def index():
    # Grouped, and only what this user may actually run. A group with
    # nothing left in it is dropped rather than rendered empty.
    allowed = [op for op in OPERATIONS if has_permission(op.permission)]
    groups = [
        (label, [op for op in allowed if op.group == key])
        for key, label in GROUPS
    ]
    return render_template(
        "accounting_ops/index.html",
        groups=[(label, ops) for label, ops in groups if ops],
        operations=allowed,
    )


@bp.route("/<op_key>", methods=["GET", "POST"])
@login_required
@require_permission("journals.create")
def run(op_key):
    op = get_operation(op_key)
    if not op:
        flash("عملية غير معروفة", "error")
        return redirect(url_for("accounting_ops.index"))

    # §6 — the real gate. Before GET renders anything and before POST
    # executes, because hiding the card on the index is presentation, not
    # protection: the URL is guessable and this is the only thing standing
    # between a curious user and a posted journal.
    if not has_permission(op.permission):
        flash("ليس لديك صلاحية لهذا الإجراء", "error")
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
