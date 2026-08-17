"""merge duplicate parallel-heads mergepoints (56895d90a5f4 + 9a3c60297454)

Revision ID: e0be265aefe1
Revises: 56895d90a5f4, 9a3c60297454
Create Date: 2026-08-17 18:05:04.643630

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e0be265aefe1'
down_revision = ('56895d90a5f4', '9a3c60297454')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
