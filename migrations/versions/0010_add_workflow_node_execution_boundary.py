"""add replay-safe workflow execution boundaries

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_stage_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("agent_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(agent_spec_hash) = 64", name="ck_run_stage_agent_spec_hash"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "stage", name="uq_run_stage_attempt"),
    )
    op.create_index(
        "ix_run_stage_attempts_run",
        "run_stage_attempts",
        ["run_id", "stage"],
        unique=False,
    )

    op.drop_constraint("uq_model_call_attempt_turn", "model_calls", type_="unique")
    op.drop_constraint("model_calls_task_id_fkey", "model_calls", type_="foreignkey")
    op.drop_constraint("model_calls_attempt_id_fkey", "model_calls", type_="foreignkey")
    op.alter_column("model_calls", "task_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("model_calls", "attempt_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(
        "model_calls_task_id_fkey",
        "model_calls",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "model_calls_attempt_id_fkey",
        "model_calls",
        "task_attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "model_calls",
        sa.Column("run_stage_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "model_calls_run_stage_attempt_id_fkey",
        "model_calls",
        "run_stage_attempts",
        ["run_stage_attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_model_call_attempt_scope",
        "model_calls",
        "(attempt_id IS NOT NULL AND run_stage_attempt_id IS NULL AND task_id IS NOT NULL) "
        "OR (attempt_id IS NULL AND run_stage_attempt_id IS NOT NULL AND task_id IS NULL)",
    )
    op.create_unique_constraint(
        "uq_model_call_invocation_turn",
        "model_calls",
        ["run_id", "trace_id", "turn"],
    )

    op.create_table(
        "workflow_node_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_workflow_node_execution_status",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["task_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "node_name", name="uq_workflow_node_attempt_name"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_workflow_node_task_status",
        "workflow_node_executions",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_node_task_status", table_name="workflow_node_executions"
    )
    op.drop_table("workflow_node_executions")

    op.drop_constraint("uq_model_call_invocation_turn", "model_calls", type_="unique")
    op.drop_constraint("ck_model_call_attempt_scope", "model_calls", type_="check")
    op.execute("DELETE FROM model_calls WHERE run_stage_attempt_id IS NOT NULL")
    op.drop_constraint(
        "model_calls_run_stage_attempt_id_fkey", "model_calls", type_="foreignkey"
    )
    op.drop_column("model_calls", "run_stage_attempt_id")
    op.drop_constraint("model_calls_task_id_fkey", "model_calls", type_="foreignkey")
    op.drop_constraint("model_calls_attempt_id_fkey", "model_calls", type_="foreignkey")
    op.alter_column("model_calls", "task_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("model_calls", "attempt_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "model_calls_task_id_fkey",
        "model_calls",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "model_calls_attempt_id_fkey",
        "model_calls",
        "task_attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_model_call_attempt_turn", "model_calls", ["attempt_id", "turn"]
    )

    op.drop_index("ix_run_stage_attempts_run", table_name="run_stage_attempts")
    op.drop_table("run_stage_attempts")
