"""merge heads: signup-auto-block + approval-gated-superadmin

Revision ID: 9a3c60297454
Revises: h4i7j0k3l6m9, i5j8k1l4m7n0
Create Date: 2026-08-12 18:44:15.068667

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a3c60297454'
down_revision = ('h4i7j0k3l6m9', 'i5j8k1l4m7n0')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
