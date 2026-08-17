"""merge heads: qty3dp + main

Revision ID: 31a4ffc232e8
Revises: j7k0l3m6n9p2, e0be265aefe1
Create Date: 2026-08-18 01:37:52.272680

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '31a4ffc232e8'
down_revision = ('j7k0l3m6n9p2', 'e0be265aefe1')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
