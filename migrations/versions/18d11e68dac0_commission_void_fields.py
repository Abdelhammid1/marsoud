"""MARSOUD-COMM-DASHBOARD — cancel/void fields on sales_commissions.

Adds three nullable fields to sales_commissions:
  * voided_at        (DateTime) — when the reversal happened.
  * voided_by_id     (User FK)  — who authorized it.
  * void_reason      (Text)     — mandatory input from the operator.

The rest of the audit trail lives in journal_entries (the reversal JE
is already tagged source_type='commission_void' + source_id=<comm.id>
by the void_commission service), so these three fields are pure
convenience — they let the list view filter/label voided rows without
joining to journal_entries.

Revision ID: 18d11e68dac0
Revises: 8a63ad9bca7e
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa

revision = "18d11e68dac0"
down_revision = "8a63ad9bca7e"
branch_labels = None
depends_on = None


def _existing():
    insp = sa.inspect(op.get_bind())
    if "sales_commissions" not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns("sales_commissions")}


def upgrade():
    have = _existing()
    with op.batch_alter_table("sales_commissions") as batch:
        if "voided_at" not in have:
            batch.add_column(sa.Column("voided_at", sa.DateTime(),
                                        nullable=True))
        if "voided_by_id" not in have:
            batch.add_column(sa.Column(
                "voided_by_id", sa.Integer(),
                sa.ForeignKey("users.id",
                              name="fk_commission_voided_by"),
                nullable=True))
        if "void_reason" not in have:
            batch.add_column(sa.Column("void_reason", sa.Text(),
                                        nullable=True))


def downgrade():
    have = _existing()
    with op.batch_alter_table("sales_commissions") as batch:
        for col in ("void_reason", "voided_by_id", "voided_at"):
            if col in have:
                batch.drop_column(col)
