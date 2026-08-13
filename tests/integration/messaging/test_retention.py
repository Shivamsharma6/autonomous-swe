from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from domain.enums import ApprovalStatus
from messaging.redis_streams import RedisStreamsTransport
from messaging.retention import RetentionPolicy, RetentionService
from persistence.tables import (
    AgentMessageRow,
    ApprovalRow,
    AuditEventRow,
    DeadLetterRow,
    MemoryCandidateRow,
    ToolExecutionRow,
)
from tests.integration.messaging.helpers import seed_task


@pytest.mark.asyncio
async def test_retention_purges_only_expired_unprotected_records(
    database: Any, redis_client: Redis
) -> None:
    terminal = await seed_task(database, run_state="COMPLETED")
    active = await seed_task(database, run_state="RUNNING")
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    purge_id, active_id, audited_id, promotion_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with database.transaction() as session:
        for message_id, ids in (
            (purge_id, terminal),
            (active_id, active),
            (audited_id, terminal),
            (promotion_id, terminal),
        ):
            session.add(
                AgentMessageRow(
                    id=message_id,
                    run_id=ids["run_id"],
                    task_id=ids["task_id"],
                    attempt_id=ids["attempt_id"],
                    kind="context_handoff",
                    schema_version="1.0",
                    sender="planner",
                    recipient="coder",
                    causation_id=uuid4(),
                    correlation_id=ids["run_id"],
                    artifact_ids=[],
                    payload={"message_id": str(message_id), "secret": "retained only if needed"},
                    content_hash="d" * 64,
                    created_at=old,
                )
            )
        session.add(
            AuditEventRow(
                id=uuid4(),
                event_type="evidence.retained",
                aggregate_type="message",
                aggregate_id=audited_id,
                payload={"message_id": str(audited_id)},
                correlation_id=terminal["run_id"],
                causation_id=uuid4(),
                content_hash="e" * 64,
                created_at=old,
            )
        )
        session.add(
            MemoryCandidateRow(
                id=uuid4(),
                project_id=terminal["project_id"],
                run_id=terminal["run_id"],
                task_id=terminal["task_id"],
                attempt_id=terminal["attempt_id"],
                classification="PROJECT",
                content="promote this",
                candidate={"source_message_id": str(promotion_id)},
                status="PROMOTING",
                created_at=old,
            )
        )
        session.add_all(
            [
                DeadLetterRow(
                    event_id=uuid4(),
                    consumer="resolved-old",
                    topic="agent-events",
                    payload={},
                    attempts=8,
                    last_error="old",
                    causation_chain=[],
                    created_at=now - timedelta(days=40),
                    resolved_at=now - timedelta(days=31),
                ),
                DeadLetterRow(
                    event_id=uuid4(),
                    consumer="unresolved",
                    topic="agent-events",
                    payload={},
                    attempts=8,
                    last_error="still active",
                    causation_chain=[],
                    created_at=now - timedelta(days=40),
                ),
            ]
        )
    transport = RedisStreamsTransport(redis_client)
    old_stream_id = f"{int((now - timedelta(days=2)).timestamp() * 1000)}-0"
    recent_stream_id = f"{int(now.timestamp() * 1000)}-0"
    await redis_client.xadd("agent-events", {"event_id": str(uuid4())}, id=old_stream_id)
    await redis_client.xadd("agent-events", {"event_id": str(uuid4())}, id=recent_stream_id)
    service = RetentionService(database, transport, policy=RetentionPolicy())

    result = await service.enforce(now=now, streams=("agent-events",))

    assert result.message_payloads_purged == 1
    assert result.dead_letters_deleted == 1
    assert result.redis_entries_deleted == 1
    async with database.transaction() as session:
        messages = {row.id: row for row in (await session.scalars(select(AgentMessageRow))).all()}
        dead_letters = tuple((await session.scalars(select(DeadLetterRow))).all())
    assert messages[purge_id].payload == {}
    assert messages[purge_id].payload_purged_at is not None
    assert messages[active_id].payload != {}
    assert messages[audited_id].payload != {}
    assert messages[promotion_id].payload != {}
    assert len(dead_letters) == 1
    assert dead_letters[0].resolved_at is None


@pytest.mark.asyncio
async def test_operational_retention_protects_active_runs_and_unresolved_approvals(
    database: Any, redis_client: Redis
) -> None:
    terminal = await seed_task(database, run_state="COMPLETED")
    active = await seed_task(database, run_state="RUNNING")
    now = datetime.now(UTC)
    old = now - timedelta(days=366)
    resolved_call, pending_call, active_call = uuid4(), uuid4(), uuid4()
    resolved_approval, pending_approval = uuid4(), uuid4()
    removable_audit, active_audit = uuid4(), uuid4()
    async with database.transaction() as session:
        for call_id, ids, key in (
            (resolved_call, terminal, "resolved"),
            (pending_call, terminal, "pending"),
            (active_call, active, "active"),
        ):
            session.add(
                ToolExecutionRow(
                    id=call_id,
                    run_id=ids["run_id"],
                    task_id=ids["task_id"],
                    attempt_id=ids["attempt_id"],
                    requested_by="coder",
                    tool_name="run_tests",
                    arguments={},
                    idempotency_key=f"retention:{key}:{call_id}",
                    status="COMPLETED",
                    result={},
                    created_at=old,
                    completed_at=old,
                )
            )
        await session.flush()
        session.add_all(
            [
                ApprovalRow(
                    id=resolved_approval,
                    call_id=resolved_call,
                    project_id=terminal["project_id"],
                    repository_id=terminal["repository_id"],
                    baseline_commit="b" * 40,
                    call_hash="1" * 64,
                    status=ApprovalStatus.APPROVED,
                    expires_at=old,
                    created_at=old,
                    decided_at=old,
                    approver="operator",
                ),
                ApprovalRow(
                    id=pending_approval,
                    call_id=pending_call,
                    project_id=terminal["project_id"],
                    repository_id=terminal["repository_id"],
                    baseline_commit="b" * 40,
                    call_hash="2" * 64,
                    status=ApprovalStatus.PENDING,
                    expires_at=now + timedelta(days=1),
                    created_at=old,
                ),
                AuditEventRow(
                    id=removable_audit,
                    event_type="expired.metadata",
                    aggregate_type="run",
                    aggregate_id=terminal["run_id"],
                    payload={},
                    correlation_id=terminal["run_id"],
                    causation_id=uuid4(),
                    content_hash="3" * 64,
                    created_at=old,
                ),
                AuditEventRow(
                    id=active_audit,
                    event_type="active.metadata",
                    aggregate_type="run",
                    aggregate_id=active["run_id"],
                    payload={},
                    correlation_id=active["run_id"],
                    causation_id=uuid4(),
                    content_hash="4" * 64,
                    created_at=old,
                ),
            ]
        )
    service = RetentionService(
        database,
        RedisStreamsTransport(redis_client),
        policy=RetentionPolicy(),
    )

    result = await service.enforce(now=now)

    assert result.operational_rows_deleted == 3
    async with database.transaction() as session:
        assert await session.get(ApprovalRow, resolved_approval) is None
        assert await session.get(ToolExecutionRow, resolved_call) is None
        assert await session.get(AuditEventRow, removable_audit) is None
        assert await session.get(ApprovalRow, pending_approval) is not None
        assert await session.get(ToolExecutionRow, pending_call) is not None
        assert await session.get(ToolExecutionRow, active_call) is not None
        assert await session.get(AuditEventRow, active_audit) is not None
        assert await session.scalar(select(func.count()).select_from(ApprovalRow)) == 1
