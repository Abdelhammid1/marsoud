"""merge subdomain + invoice-orphan-cascade heads

Revision ID: b6be6631d1cb
Revises: a6c9f2e5b8d1, d5133e40815c
Create Date: 2026-07-22 09:26:38.752184

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6be6631d1cb'
down_revision = ('a6c9f2e5b8d1', 'd5133e40815c')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
