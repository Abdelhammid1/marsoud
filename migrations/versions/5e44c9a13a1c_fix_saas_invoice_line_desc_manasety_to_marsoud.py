"""MARSOUD-TKT-SAAS-INVOICE-LINE-LABEL — retroactive SaaS invoice
line description fix.

Every SaaS billing invoice line was seeded with the label
"اشتراك منصتي — باقة …" — which is wrong on two counts: the
customer subscribes to "مرصود" the product, not "منصتي" the
company; and the ticket AC asks for a UNIFIED label
"اشتراك مرصود — باقة …" across the app.

Fixed at the source in `app/services/saas_billing.py`. This
migration retroactively rewrites existing InvoiceItem rows so
the customer's invoice history stops showing the old wording.

Idempotent: the string it looks for is the exact old label; a
re-run on an already-migrated DB is a no-op.

Revision ID: 5e44c9a13a1c
Revises: 02e70940195c
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa

revision = "5e44c9a13a1c"
down_revision = "02e70940195c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "UPDATE invoice_items "
        "SET description = REPLACE(description, "
        "  'اشتراك منصتي — باقة', 'اشتراك مرصود — باقة') "
        "WHERE description LIKE '%اشتراك منصتي — باقة%'"
    ))


def downgrade():
    op.execute(sa.text(
        "UPDATE invoice_items "
        "SET description = REPLACE(description, "
        "  'اشتراك مرصود — باقة', 'اشتراك منصتي — باقة') "
        "WHERE description LIKE '%اشتراك مرصود — باقة%'"
    ))
