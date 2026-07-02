"""MARSOUD-EMPLOYEE-DAILY-REPORTS — digest tables.

Revision ID: d2_c9f4a8e2b6d
Revises: d1_b8f2c4a7e3d
"""
from alembic import op
import sqlalchemy as sa


revision = "d2_c9f4a8e2b6d"
down_revision = "d1_b8f2c4a7e3d"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "employee_daily_reports" not in insp.get_table_names():
        op.create_table(
            "employee_daily_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer(),
                      sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("report_date", sa.Date(), nullable=False, index=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("employee_notes", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20),
                      nullable=False, server_default="DRAFT", index=True),
            sa.Column("submitted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp(),
                      nullable=False),
            sa.UniqueConstraint(
                "company_id", "employee_id", "report_date",
                name="uq_employee_daily_report_day",
            ),
        )

    if "employee_report_access" not in insp.get_table_names():
        op.create_table(
            "employee_report_access",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("viewer_user_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer(),
                      sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint(
                "company_id", "viewer_user_id", "employee_id",
                name="uq_employee_report_access",
            ),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "employee_report_access" in insp.get_table_names():
        op.drop_table("employee_report_access")
    if "employee_daily_reports" in insp.get_table_names():
        op.drop_table("employee_daily_reports")
