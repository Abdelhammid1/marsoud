"""HR-SS — User.status + User.employee_id + employee_history

Revision ID: l9f7d3a5b2c8
Revises: k8e6c2f9d4a3
Create Date: 2026-06-11 16:00:00

Schema for the HR Self-Service ticket.

  - users.status       — ACTIVE / PENDING / DISABLED (default ACTIVE for
                          existing rows so login isn't broken).
  - users.employee_id  — FK to employees.id, nullable. Links a User row
                          back to the Employee they correspond to so the
                          self-service portal can query their data.
  - employee_history   — DEPARTMENT / JOB_TITLE / SALARY / STATUS audit
                          trail keyed by employee_id.

Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "l9f7d3a5b2c8"
down_revision = "k8e6c2f9d4a3"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    if not _has_column("users", "status"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "status", sa.String(20),
                nullable=False, server_default="ACTIVE",
            ))
    if not _has_column("users", "employee_id"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "employee_id", sa.Integer,
                sa.ForeignKey("employees.id",
                              name="fk_users_employee_id",
                              ondelete="SET NULL"),
                nullable=True,
            ))

    if not _has_table("employee_history"):
        op.create_table(
            "employee_history",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("employee_id", sa.Integer,
                      sa.ForeignKey("employees.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("change_type", sa.String(30), nullable=False),
            sa.Column("old_value", sa.String(255)),
            sa.Column("new_value", sa.String(255)),
            sa.Column("changed_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("changed_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False, index=True),
            sa.Column("notes", sa.String(255)),
        )


def downgrade():
    if _has_table("employee_history"):
        try:
            op.drop_table("employee_history")
        except Exception:
            pass
    # Best-effort column drop — SQLite needs batch mode.
    if _has_column("users", "employee_id"):
        try:
            with op.batch_alter_table("users") as batch:
                batch.drop_column("employee_id")
        except Exception:
            pass
    if _has_column("users", "status"):
        try:
            with op.batch_alter_table("users") as batch:
                batch.drop_column("status")
        except Exception:
            pass
