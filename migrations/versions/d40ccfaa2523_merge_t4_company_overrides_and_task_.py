"""merge T4-company-overrides and task-hierarchy/ops-hub heads

Revision ID: d40ccfaa2523
Revises: 5f628291f4c7, c1e2f3a4b5d6
Create Date: 2026-08-09 07:37:58.083201

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd40ccfaa2523'
down_revision = ('5f628291f4c7', 'c1e2f3a4b5d6')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
