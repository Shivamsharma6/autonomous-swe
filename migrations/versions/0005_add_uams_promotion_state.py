"""add external UAMS promotion state

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memory_candidates", sa.Column("uams_memory_id", sa.UUID()))
    op.add_column("memory_candidates", sa.Column("uams_revision_id", sa.String(length=200)))
    op.add_column("memory_candidates", sa.Column("uams_searchable_at", sa.DateTime(timezone=True)))
    op.add_column("memory_candidates", sa.Column("last_error", sa.Text()))
    op.create_index("ix_memory_candidates_uams_memory", "memory_candidates", ["uams_memory_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_candidates_uams_memory", table_name="memory_candidates")
    op.drop_column("memory_candidates", "last_error")
    op.drop_column("memory_candidates", "uams_searchable_at")
    op.drop_column("memory_candidates", "uams_revision_id")
    op.drop_column("memory_candidates", "uams_memory_id")
