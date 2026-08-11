"""MARSOUD-PROJECT-ARCHIVE (2026-08-10) — projects.archived_at.

Adds `archived_at` (DateTime, indexed) + `archived_by_id`
(nullable FK -> users) to the projects table so a project can
be "put away" once finished, orthogonally to its status. Both
columns default NULL — no plan or project on any DB gets
archived on migration; the /projects/<id>/archive POST is the
only writer.

Idempotent — `_has_col` + `_has_index` guards mirror the
task-archive migration (c0_7d3b9e2f4a1_task_archive.py) so a
rerun is a no-op.

Revision ID: g3h6i9j2k5l8
Revises: e2f5a8c1b4d7
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa


revision = "g3h6i9j2k5l8"
down_revision = "e2f5a8c1b4d7"
branch_labels = None
depends_on = None


TABLE = "projects"
COL_AT = "archived_at"
COL_BY = "archived_by_id"
IDX = "ix_projects_archived_at"
FK = "fk_projects_archived_by_id"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table, name):
    return any(ix["name"] == name for ix in _inspector().get_indexes(table))


def upgrade():
    if not _has_col(TABLE, COL_AT) or not _has_col(TABLE, COL_BY):
        with op.batch_alter_table(TABLE) as batch:
            if not _has_col(TABLE, COL_AT):
                batch.add_column(sa.Column(
                    COL_AT, sa.DateTime, nullable=True))
            if not _has_col(TABLE, COL_BY):
                batch.add_column(sa.Column(
                    COL_BY, sa.Integer,
                    sa.ForeignKey("users.id", name=FK),
                    nullable=True,
                ))
    if _has_col(TABLE, COL_AT) and not _has_index(TABLE, IDX):
        op.create_index(IDX, TABLE, [COL_AT])


def downgrade():
    if _has_col(TABLE, COL_AT):
        try:
            op.drop_index(IDX, table_name=TABLE)
        except Exception:
            pass
        with op.batch_alter_table(TABLE) as batch:
            try:
                batch.drop_constraint(FK, type_="foreignkey")
            except Exception:
                pass
            if _has_col(TABLE, COL_BY):
                batch.drop_column(COL_BY)
            if _has_col(TABLE, COL_AT):
                batch.drop_column(COL_AT)
