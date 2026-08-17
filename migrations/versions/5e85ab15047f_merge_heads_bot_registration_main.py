"""merge heads: bot-registration + main

Revision ID: 5e85ab15047f
Revises: m0n3o6p9q2r5, 31a4ffc232e8
Create Date: 2026-08-18 01:43:50.463492

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5e85ab15047f'
down_revision = ('m0n3o6p9q2r5', '31a4ffc232e8')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
