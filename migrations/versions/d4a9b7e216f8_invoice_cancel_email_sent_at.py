"""invoices.cancel_email_sent_at for cancel-notification idempotency

MARSOUD-INVOICE-CANCEL-EMAIL-01 (2026-10-18) — stamps when the
"invoice cancelled" notification landed in the customer's inbox so
a retry / double-POST on the /resend-cancel-email route cannot fire
a second notification for the same void.  NULL means either the
email never fired (customer had no address, SMTP down) or the
invoice is DRAFT (never notified anyway).

NULL for every existing invoice; code treats absence as "not sent".

Revision ID: d4a9b7e216f8
Revises: c3f8e42a9b17
Create Date: 2026-10-18 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "d4a9b7e216f8"
down_revision = "c3f8e42a9b17"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("cancel_email_sent_at",
                                     sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("cancel_email_sent_at")
