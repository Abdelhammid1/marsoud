"""CRM + Projects + Tasks — native Cycle 7

Revision ID: f1a4c9e23bd5
Revises: e9c6d3185f48
Create Date: 2026-06-08 16:00:00

Creates 7 new tables, all `company_id`-scoped:
  - leads, lead_status_events                       (CRM Pipeline)
  - projects, project_members, milestones,
    project_status_events                           (Project Management)
  - tasks                                           (Task Kanban)

Reuses Marsoud's existing `users` (for staff/assignees/managers) and
`customers` (for project clients) — no parallel user system. Lead → Won
→ Convert flow auto-creates a Customer row in the existing customers table.

All FK columns added as plain Integer (no inline ForeignKey in the migration
itself) to dodge SQLite batch-mode's "Constraint must have a name" — the ORM
models still declare relationships.
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a4c9e23bd5"
down_revision = "e9c6d3185f48"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    # ─── leads ───────────────────────────────────────────────────────────
    if not _has_table("leads"):
        op.create_table(
            "leads",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("client_name", sa.String(150), nullable=False),
            sa.Column("email", sa.String(200)),
            sa.Column("phone", sa.String(30), nullable=False),
            sa.Column("service_needed", sa.String(200), nullable=False),
            sa.Column("source", sa.String(100)),
            sa.Column("assigned_to_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("status", sa.String(30), nullable=False,
                      server_default="NEW_LEAD", index=True),
            sa.Column("next_meeting", sa.DateTime),
            sa.Column("meeting_notes", sa.Text),
            sa.Column("quotation_path", sa.Text),
            sa.Column("contract_path", sa.Text),
            sa.Column("lost_reason", sa.Text),
            sa.Column("converted_at", sa.DateTime),
            sa.Column("converted_customer_id", sa.Integer,
                      sa.ForeignKey("customers.id")),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    # ─── lead_status_events ──────────────────────────────────────────────
    if not _has_table("lead_status_events"):
        op.create_table(
            "lead_status_events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("lead_id", sa.Integer,
                      sa.ForeignKey("leads.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("from_status", sa.String(30)),
            sa.Column("to_status", sa.String(30), nullable=False),
            sa.Column("changed_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("note", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    # ─── projects ────────────────────────────────────────────────────────
    if not _has_table("projects"):
        op.create_table(
            "projects",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("lead_id", sa.Integer, sa.ForeignKey("leads.id")),
            sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"),
                      nullable=False),
            sa.Column("type", sa.String(100), nullable=False),
            sa.Column("manager_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("start_date", sa.Date, nullable=False),
            sa.Column("end_date", sa.Date, nullable=False),
            sa.Column("status", sa.String(30), nullable=False,
                      server_default="PLANNING", index=True),
            sa.Column("progress_pct", sa.Numeric(5, 2), nullable=False,
                      server_default="0"),
            sa.Column("notes", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    # ─── project_members ─────────────────────────────────────────────────
    if not _has_table("project_members"):
        op.create_table(
            "project_members",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("project_id", sa.Integer,
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("added_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.UniqueConstraint("project_id", "user_id",
                                name="uq_project_member"),
        )

    # ─── milestones ──────────────────────────────────────────────────────
    if not _has_table("milestones"):
        op.create_table(
            "milestones",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("project_id", sa.Integer,
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("target_date", sa.Date),
            sa.Column("order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("completed_at", sa.DateTime),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    # ─── project_status_events ───────────────────────────────────────────
    if not _has_table("project_status_events"):
        op.create_table(
            "project_status_events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("project_id", sa.Integer,
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("from_status", sa.String(30)),
            sa.Column("to_status", sa.String(30), nullable=False),
            sa.Column("changed_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("note", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    # ─── tasks ───────────────────────────────────────────────────────────
    if not _has_table("tasks"):
        op.create_table(
            "tasks",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("project_id", sa.Integer,
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("milestone_id", sa.Integer,
                      sa.ForeignKey("milestones.id", ondelete="SET NULL")),
            sa.Column("assigned_to_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("priority", sa.String(20), nullable=False,
                      server_default="MEDIUM"),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="TODO", index=True),
            sa.Column("deadline", sa.Date),
            sa.Column("notes", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
            sa.Column("completed_at", sa.DateTime),
        )


def downgrade():
    for table in (
        "tasks", "project_status_events", "milestones", "project_members",
        "projects", "lead_status_events", "leads",
    ):
        if _has_table(table):
            try:
                op.drop_table(table)
            except Exception:
                pass
