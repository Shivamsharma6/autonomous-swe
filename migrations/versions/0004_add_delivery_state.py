"""add durable message delivery state

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND OLD.created_at <= now() - interval '365 days'
               AND NOT EXISTS (
                   SELECT 1 FROM runs
                   WHERE runs.id = OLD.correlation_id
                     AND runs.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM approvals
                   WHERE OLD.aggregate_type = 'approval'
                     AND approvals.id = OLD.aggregate_id
                     AND approvals.status = 'PENDING'
               )
               AND NOT EXISTS (
                   SELECT 1 FROM memory_candidates
                   WHERE OLD.aggregate_type = 'memory_candidate'
                     AND memory_candidates.id = OLD.aggregate_id
                     AND memory_candidates.status IN (
                         'PENDING', 'PROMOTING', 'WAITING_FOR_MEMORY'
                     )
               )
            THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_events are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.add_column("agent_messages", sa.Column("payload_purged_at", sa.DateTime(timezone=True)))
    op.add_column("outbox", sa.Column("publisher", sa.String(length=255)))
    op.add_column("outbox", sa.Column("claim_token", sa.UUID()))
    op.add_column("outbox", sa.Column("claimed_until", sa.DateTime(timezone=True)))
    op.create_index("ix_outbox_claim_expiry", "outbox", ["claimed_until"])
    op.add_column("dead_letters", sa.Column("consumer", sa.String(length=255)))
    op.execute("UPDATE dead_letters SET consumer = 'legacy' WHERE consumer IS NULL")
    op.alter_column("dead_letters", "consumer", nullable=False)
    op.create_unique_constraint(
        "uq_dead_letter_consumer_event", "dead_letters", ["consumer", "event_id"]
    )
    op.create_table(
        "consumer_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("consumer", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_consumer_delivery_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consumer", "event_id", name="uq_consumer_event_delivery"),
    )
    op.create_index(
        "ix_consumer_delivery_retry",
        "consumer_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_consumer_delivery_retry", table_name="consumer_deliveries")
    op.drop_table("consumer_deliveries")
    op.drop_constraint("uq_dead_letter_consumer_event", "dead_letters", type_="unique")
    op.drop_column("dead_letters", "consumer")
    op.drop_index("ix_outbox_claim_expiry", table_name="outbox")
    op.drop_column("outbox", "claimed_until")
    op.drop_column("outbox", "claim_token")
    op.drop_column("outbox", "publisher")
    op.drop_column("agent_messages", "payload_purged_at")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
