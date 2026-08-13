"""add project task resource estimates

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_task_resource_estimates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "task_type",
            postgresql.ENUM(
                "RESEARCH",
                "IMPLEMENTATION",
                "TEST",
                "REFACTOR",
                "DOCUMENTATION",
                "VALIDATION",
                name="task_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("average_cpu_time_ms", sa.Float(), nullable=False),
        sa.Column("peak_memory_bytes", sa.BigInteger(), nullable=False),
        sa.Column("average_duration_ms", sa.Float(), nullable=False),
        sa.Column("average_output_bytes", sa.Float(), nullable=False),
        sa.Column("average_network_requests", sa.Float(), nullable=False),
        sa.Column("average_model_tokens", sa.Float(), nullable=False),
        sa.Column("average_cost_usd", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sample_count >= 0", name="ck_resource_estimate_sample_count"),
        sa.CheckConstraint(
            "average_cpu_time_ms >= 0 AND peak_memory_bytes >= 0 "
            "AND average_duration_ms >= 0 AND average_output_bytes >= 0 "
            "AND average_network_requests >= 0 AND average_model_tokens >= 0 "
            "AND average_cost_usd >= 0",
            name="ck_resource_estimates_nonnegative",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "task_type", name="uq_project_task_resource_estimate"),
    )


def downgrade() -> None:
    op.drop_table("project_task_resource_estimates")
