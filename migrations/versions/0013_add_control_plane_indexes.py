"""Index the access paths the control plane and scheduler actually use.

Revision ID: 0013
Revises: 0012

Every index here was missing while a query depended on it.

* ``audit_events`` carries a row for every state transition, tool call, approval
  decision, and reconciliation action, and had no index on ``correlation_id``.
  ``GET /runs/{id}/events`` filters on exactly that column, and the task event
  WebSocket polled it twice a second per connection. Both were sequential scans
  of the largest table in the schema.
* ``tasks(state, state_entered_at)`` backs the reconciliation scan, which filters
  on ``state`` and orders by ``state_entered_at``. In steady state the row count
  is small, but the failure mode is a mass wedge, and with no qualifying row
  ``LIMIT`` gives no early exit, so every cycle evaluated the predicate against
  every stuck task.
* ``plan_revisions(run_id)`` backs the ``max(revision)`` lookup performed once
  per run by the run list endpoint.
* ``outbox`` had a full index on ``(published_at, next_attempt_at)`` while the
  claim query filters on ``published_at IS NULL AND dead_lettered_at IS NULL``,
  orders by ``created_at``, and takes the oldest entries. The index could not
  supply that order, and it carried an entry for every published row forever.
  A partial index over only the publishable rows matches the claim.

CONCURRENTLY is used for the large tables so the build does not take an
ACCESS EXCLUSIVE lock for its duration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("ix_audit_correlation_created", "audit_events", "(correlation_id, created_at, id)"),
    ("ix_audit_created", "audit_events", "(created_at, id)"),
    ("ix_tasks_state_entered", "tasks", "(state, state_entered_at)"),
    ("ix_plan_revisions_run", "plan_revisions", "(run_id)"),
)


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block, and Alembic
    # wraps each migration in one, so the statements are issued in an autocommit
    # block. Without that the migration fails outright.
    with op.get_context().autocommit_block():
        for name, table, columns in _INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {columns}"
            )
        # Partial: only rows that can still be claimed, ordered the way the claim
        # query reads them.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_outbox_publishable_partial "
            "ON outbox (next_attempt_at, created_at, event_id) "
            "WHERE published_at IS NULL AND dead_lettered_at IS NULL"
        )
        # Superseded: it could not supply the claim query's ordering and carried
        # an entry for every published row forever.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_outbox_publishable")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_outbox_publishable_partial")
        for name, _table, _columns in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_outbox_publishable "
            "ON outbox (published_at, next_attempt_at)"
        )
