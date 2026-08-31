"""MARSOUD-TKT-ADMIN-OWNER-COL — customers.contact_person

Adds a nullable contact_person column to `customers`. Read/written by
the tenant-side /customers page which now shows this next to the
customer name and links it to the customer view page.

Existing rows land with NULL — the template renders '—' for that.

Revision ID: 02e70940195c
Revises: c68a8e80606b
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa

revision = "02e70940195c"
down_revision = "c68a8e80606b"
branch_labels = None
depends_on = None


def _has_col():
    insp = sa.inspect(op.get_bind())
    if "customers" not in insp.get_table_names():
        return False
    return "contact_person" in {
        c["name"] for c in insp.get_columns("customers")}


def upgrade():
    if _has_col():
        return
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column(
            "contact_person", sa.String(150), nullable=True))


def downgrade():
    if not _has_col():
        return
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("contact_person")
