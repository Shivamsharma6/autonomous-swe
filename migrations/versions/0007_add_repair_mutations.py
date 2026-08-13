"""add durable repair mutation decisions

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repair_mutations",
        sa.Column("mutation_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("accepted_revision", sa.Integer()),
        sa.Column("failure_signature", sa.String(length=64), nullable=False),
        sa.Column("progress_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("mutation_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "verification_artifact_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "mutation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "accepted_revision IS NULL OR accepted_revision > base_revision",
            name="ck_repair_accepted_revision_order",
        ),
        sa.CheckConstraint("base_revision >= 1", name="ck_repair_base_revision_positive"),
        sa.CheckConstraint("length(failure_signature) = 64", name="ck_repair_failure_signature"),
        sa.CheckConstraint("length(mutation_hash) = 64", name="ck_repair_mutation_hash"),
        sa.CheckConstraint(
            "length(progress_fingerprint) = 64",
            name="ck_repair_progress_fingerprint",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("mutation_id"),
    )
    op.create_index(
        "ix_repair_mutations_run_status",
        "repair_mutations",
        ["run_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_repair_mutations_run_status", table_name="repair_mutations")
    op.drop_table("repair_mutations")
