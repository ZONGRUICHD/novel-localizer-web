"""Add an explicit upload purpose for books and replacement covers.

Revision ID: 0002_upload_purpose
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_upload_purpose"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("uploads")}
    if "purpose" not in columns:
        with op.batch_alter_table("uploads") as batch:
            batch.add_column(
                sa.Column("purpose", sa.String(length=16), nullable=False, server_default="book")
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("uploads")}
    if "purpose" in columns:
        with op.batch_alter_table("uploads") as batch:
            batch.drop_column("purpose")
