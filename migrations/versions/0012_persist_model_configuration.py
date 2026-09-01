"""Persist workspace model settings and new-run snapshots.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_model_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_workspace_model_config_singleton"),
    )
    op.add_column("runs", sa.Column("model_configuration", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "model_configuration")
    op.drop_table("workspace_model_config")
