"""MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — grow the plan sub-item
catalog by 26 rows without changing what any existing plan shows.

The catalog widening in plan_gating.py::SUB_ITEM_CATALOG makes 26
new endpoints togglable per plan. Nine of them were previously
UNGATED entirely (endpoint_to_subitem returned None → subitem_allowed
short-circuited True for every plan); the other 17 were "lumped
under a parent" (e.g. inventory.adjust → inventory.index).

Without a backfill, any plan with a non-NULL allowed_subitems would
lose visibility of those endpoints the moment the catalog grows —
they wouldn't appear in the plan's stored list, so subitem_allowed
would refuse. This migration appends only what was ALREADY visible
for each plan:

  · ALWAYS_APPEND (the 9 ungated ones) → appended to every
    non-NULL plan's list.
  · LUMPED_UNDER (17 endpoints whose visibility today follows a
    parent) → appended only when the parent is present in the
    plan's list.

NULL plans stay NULL (semantic = "all allowed" per Plan.subitems
at plan.py:53-60; the new rows inherit visibility for free).

Data-only, no schema change. Idempotent — the merge is a set
union and only UPDATEs rows that would actually grow.

Revision ID: f8c2e5a9d4b1
Revises: 212eb02cf7c6
Create Date: 2026-08-09
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "f8c2e5a9d4b1"
down_revision = "212eb02cf7c6"
branch_labels = None
depends_on = None


# The full set of new endpoints — the migration's target space.
# downgrade() strips these from every plan's list.
NEW_ENDPOINTS = [
    "accounting_ops.index",
    "recurring_invoices.index",
    "pos.shifts", "pos.history",
    "inventory.adjust", "inventory.opening_balance",
    "inventory.movements", "inventory.transfers",
    "inventory.inventory_balance", "inventory.barcodes_picker",
    "inventory_counts.index", "products.hierarchy",
    "leads.no_response_index",
    "tasks.archive_mine",
    "hr.departments", "hr.leave_types", "hr.leave_requests",
    "hr.attendance_policies", "advances.index",
    "payroll.archive", "custody.index", "item_custody.index",
    "evaluations.index", "evaluations.logs_index",
    "settings_employee_reports.index", "settings_usage.index",
    "user_files.index",
]

# Endpoints that were UNGATED before this ticket
# (endpoint_to_subitem returned None → subitem_allowed True for
# every plan). Append to every non-NULL plan's list so their
# visibility is preserved after the catalog widens.
ALWAYS_APPEND = {
    "advances.index", "custody.index", "item_custody.index",
    "evaluations.index", "evaluations.logs_index",
    "inventory_counts.index", "recurring_invoices.index",
    "settings_employee_reports.index", "settings_usage.index",
    "user_files.index",
}

# Endpoints that were VISIBLE-IF-PARENT-VISIBLE before this
# ticket (endpoint_to_subitem lumped them under the parent
# subitem). Append only when the parent is present in the
# plan's list — otherwise the row wasn't visible for that
# plan and shouldn't become so.
LUMPED_UNDER = {
    "accounting_ops.index":         "journals.index",
    "pos.shifts":                   "pos.index",
    "pos.history":                  "pos.index",
    "inventory.adjust":             "inventory.index",
    "inventory.opening_balance":    "inventory.index",
    "inventory.movements":          "inventory.index",
    "inventory.transfers":          "inventory.index",
    "inventory.inventory_balance":  "inventory.index",
    "inventory.barcodes_picker":    "inventory.index",
    "products.hierarchy":           "products.index",
    "leads.no_response_index":      "leads.index",
    "tasks.archive_mine":           "tasks.index",
    "hr.departments":               "hr.index",
    "hr.leave_types":               "hr.index",
    "hr.leave_requests":            "hr.index",
    "hr.attendance_policies":       "hr.attendance",
    "payroll.archive":              "payroll.index",
}


def _rows():
    bind = op.get_bind()
    return list(bind.execute(sa.text(
        "SELECT id, allowed_subitems FROM plans"
    )))


def upgrade():
    bind = op.get_bind()
    for row in _rows():
        pid = row[0]
        raw = row[1]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # NULL / empty string → semantic "all allowed". New rows
            # inherit visibility; leave the column untouched.
            continue
        try:
            existing = json.loads(raw)
            if not isinstance(existing, list):
                # Corrupt column value — skip. Don't try to heal it
                # here; a super-admin will re-save the plan later.
                continue
        except (ValueError, TypeError):
            continue
        current = set(existing)
        additions = set()
        for ep in NEW_ENDPOINTS:
            if ep in current:
                continue  # idempotency — rerun is a no-op
            if ep in ALWAYS_APPEND:
                additions.add(ep)
            elif ep in LUMPED_UNDER and LUMPED_UNDER[ep] in current:
                additions.add(ep)
        if not additions:
            continue
        merged = list(existing) + sorted(additions)
        bind.execute(
            sa.text("UPDATE plans SET allowed_subitems = :v WHERE id = :i"),
            {"v": json.dumps(merged), "i": pid},
        )


def downgrade():
    bind = op.get_bind()
    strip = set(NEW_ENDPOINTS)
    for row in _rows():
        pid = row[0]
        raw = row[1]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            existing = json.loads(raw)
            if not isinstance(existing, list):
                continue
        except (ValueError, TypeError):
            continue
        trimmed = [e for e in existing if e not in strip]
        if len(trimmed) == len(existing):
            continue
        bind.execute(
            sa.text("UPDATE plans SET allowed_subitems = :v WHERE id = :i"),
            {"v": json.dumps(trimmed), "i": pid},
        )
