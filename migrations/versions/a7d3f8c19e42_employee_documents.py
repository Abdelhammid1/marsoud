"""MARSOUD-HR-EMPLOYEE-DOCS-01 — employee documents module.

Adds two tables:

  required_document_types — per-tenant catalogue of paper types HR
                            expects (national ID, health cert, etc).
                            Each tenant defines its own list.
  employee_documents      — one row per (employee, doc_type)
                            recording submitted/missing/expired
                            state + optional uploaded file blob key.

Idempotent per the `9c2e4b8f7a11_hr_decisions.py` pattern — safe
to re-run.

Revision ID: a7d3f8c19e42
Revises: f4d1b8c6a37e
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa


revision = "a7d3f8c19e42"
down_revision = "f4d1b8c6a37e"
branch_labels = None
depends_on = None


TABLE_TYPES = "required_document_types"
TABLE_DOCS = "employee_documents"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if not _has_table(TABLE_TYPES):
        op.create_table(
            TABLE_TYPES,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                       sa.ForeignKey("companies.id",
                                     name="fk_doc_type_company"),
                       nullable=False, index=True),
            sa.Column("name_ar", sa.String(150), nullable=False),
            sa.Column("is_mandatory", sa.Boolean(),
                       nullable=False, server_default=sa.true()),
            sa.Column("has_expiry", sa.Boolean(),
                       nullable=False, server_default=sa.false()),
            sa.Column("default_validity_months", sa.Integer(),
                       nullable=True),
            sa.Column("is_active", sa.Boolean(),
                       nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(),
                       nullable=False,
                       server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("company_id", "name_ar",
                                 name="uq_doc_type_company_name"),
        )

    if not _has_table(TABLE_DOCS):
        op.create_table(
            TABLE_DOCS,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                       sa.ForeignKey("companies.id",
                                     name="fk_emp_doc_company"),
                       nullable=False, index=True),
            sa.Column("employee_id", sa.Integer,
                       sa.ForeignKey("employees.id",
                                     name="fk_emp_doc_employee",
                                     ondelete="CASCADE"),
                       nullable=False, index=True),
            sa.Column("document_type_id", sa.Integer,
                       sa.ForeignKey("required_document_types.id",
                                     name="fk_emp_doc_type",
                                     ondelete="RESTRICT"),
                       nullable=False, index=True),
            sa.Column("status", sa.String(20),
                       nullable=False, server_default="SUBMITTED"),
            sa.Column("submitted_date", sa.Date(), nullable=True),
            sa.Column("expiry_date", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("file_storage_key", sa.String(200),
                       nullable=True),
            sa.Column("file_original_name", sa.String(255),
                       nullable=True),
            sa.Column("file_size_bytes", sa.Integer(),
                       nullable=True),
            sa.Column("file_mimetype", sa.String(120),
                       nullable=True),
            sa.Column("created_by_id", sa.Integer,
                       sa.ForeignKey("users.id",
                                     name="fk_emp_doc_creator"),
                       nullable=True),
            sa.Column("created_at", sa.DateTime(),
                       nullable=False,
                       server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(),
                       nullable=False,
                       server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("employee_id", "document_type_id",
                                 name="uq_employee_document_type"),
        )


def downgrade():
    if _has_table(TABLE_DOCS):
        op.drop_table(TABLE_DOCS)
    if _has_table(TABLE_TYPES):
        op.drop_table(TABLE_TYPES)
