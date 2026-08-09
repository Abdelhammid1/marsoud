"""merge T11-ops-health and T4/task-hierarchy/ops-hub heads

Revision ID: 212eb02cf7c6
Revises: d40ccfaa2523, z6y1o4x8p2q5
Create Date: 2026-08-09 07:40:17.126916

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '212eb02cf7c6'
down_revision = ('d40ccfaa2523', 'z6y1o4x8p2q5')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
