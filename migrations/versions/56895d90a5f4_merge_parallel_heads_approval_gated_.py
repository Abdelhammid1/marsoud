"""merge parallel heads (approval-gated + signup-auto-block)

Revision ID: 56895d90a5f4
Revises: h4i7j0k3l6m9, i5j8k1l4m7n0
Create Date: 2026-08-13 00:24:17.192983

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '56895d90a5f4'
down_revision = ('h4i7j0k3l6m9', 'i5j8k1l4m7n0')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
