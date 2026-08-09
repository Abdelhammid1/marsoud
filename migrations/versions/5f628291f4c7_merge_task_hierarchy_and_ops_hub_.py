"""merge task-hierarchy and ops-hub/custody-1180 heads

Revision ID: 5f628291f4c7
Revises: 1017e32a3ee2, c9d1e4f7a2b8
Create Date: 2026-08-09 07:27:56.552814

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5f628291f4c7'
down_revision = ('1017e32a3ee2', 'c9d1e4f7a2b8')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
