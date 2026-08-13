"""add exact tool contract version to executions

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_executions",
        sa.Column("tool_version", sa.String(length=20), nullable=True),
    )
    op.execute("UPDATE tool_executions SET tool_version = '1.0' WHERE tool_version IS NULL")
    op.alter_column("tool_executions", "tool_version", nullable=False)


def downgrade() -> None:
    op.drop_column("tool_executions", "tool_version")
