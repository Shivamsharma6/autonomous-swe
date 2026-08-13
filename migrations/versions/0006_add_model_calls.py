"""add durable model call accounting

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("trace_id", sa.String(length=500), nullable=False),
        sa.Column("provider_request_id", sa.String(length=500)),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("agent_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("failure_class", sa.String(length=50)),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "tool_call_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("turn >= 1", name="ck_model_call_turn_positive"),
        sa.CheckConstraint("length(agent_spec_hash) = 64", name="ck_model_call_agent_spec_hash"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND cost_usd >= 0",
            name="ck_model_call_usage_nonnegative",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "turn", name="uq_model_call_attempt_turn"),
    )
    op.create_index("ix_model_calls_trace", "model_calls", ["trace_id"])
    op.create_index("ix_model_calls_task_created", "model_calls", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_model_calls_task_created", table_name="model_calls")
    op.drop_index("ix_model_calls_trace", table_name="model_calls")
    op.drop_table("model_calls")
