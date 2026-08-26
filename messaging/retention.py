from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, select, text

from domain.enums import RUN_TERMINAL_STATES, ApprovalStatus
from messaging.redis_streams import RedisStreamsTransport
from persistence.tables import (
    AgentMessageRow,
    ApprovalRow,
    AuditEventRow,
    DeadLetterRow,
    GraphExecutionRow,
    MemoryCandidateRow,
    OutboxRow,
    RunRow,
    TaskAttemptRow,
    TaskRow,
    ToolExecutionRow,
)

TERMINAL_RUN_STATES = tuple(status.value for status in RUN_TERMINAL_STATES)
CURRENT_PROMOTION_STATES = ("PENDING", "PROMOTING", "WAITING_FOR_MEMORY")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    redis_streams: timedelta = timedelta(hours=24)
    agent_message_payloads: timedelta = timedelta(days=90)
    resolved_dead_letters: timedelta = timedelta(days=30)
    operational_metadata: timedelta = timedelta(days=365)
    checkpoint_state: timedelta = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    redis_entries_deleted: int = 0
    message_payloads_purged: int = 0
    dead_letters_deleted: int = 0
    operational_rows_deleted: int = 0
    checkpoint_rows_deleted: int = 0


class RetentionService:
    def __init__(
        self,
        database: Any,
        transport: RedisStreamsTransport,
        *,
        policy: RetentionPolicy,
    ) -> None:
        self._database = database
        self._transport = transport
        self._policy = policy

    async def enforce(
        self, *, now: datetime | None = None, streams: tuple[str, ...] = ()
    ) -> RetentionResult:
        timestamp = now or datetime.now(UTC)
        redis_deleted = 0
        for stream in streams:
            redis_deleted += await self._transport.trim_before(
                stream, timestamp - self._policy.redis_streams
            )
        (
            messages,
            dead_letters,
            operational,
            checkpoint_rows,
        ) = await self._purge_postgres(timestamp)
        return RetentionResult(
            redis_entries_deleted=redis_deleted,
            message_payloads_purged=messages,
            dead_letters_deleted=dead_letters,
            operational_rows_deleted=operational,
            checkpoint_rows_deleted=checkpoint_rows,
        )

    async def _purge_postgres(self, now: datetime) -> tuple[int, int, int, int]:
        message_cutoff = now - self._policy.agent_message_payloads
        dead_letter_cutoff = now - self._policy.resolved_dead_letters
        checkpoint_cutoff = now - self._policy.checkpoint_state
        async with self._database.transaction() as session:
            candidates = tuple(
                (
                    await session.scalars(
                        select(AgentMessageRow)
                        .join(RunRow, RunRow.id == AgentMessageRow.run_id)
                        .where(
                            AgentMessageRow.created_at <= message_cutoff,
                            AgentMessageRow.payload_purged_at.is_(None),
                            RunRow.state.in_(TERMINAL_RUN_STATES),
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            purged = 0
            for message in candidates:
                if await self._message_is_protected(session, message):
                    continue
                message.payload = {}
                message.payload_purged_at = now
                purged += 1

            result = await session.execute(
                delete(DeadLetterRow).where(
                    DeadLetterRow.resolved_at.is_not(None),
                    DeadLetterRow.resolved_at <= dead_letter_cutoff,
                    ~exists().where(
                        AuditEventRow.aggregate_type == "dead_letter",
                        AuditEventRow.aggregate_id == DeadLetterRow.id,
                    ),
                )
            )
            operational = await self._purge_operational_metadata(
                session, now - self._policy.operational_metadata
            )
            outbox_cutoff = now - self._policy.operational_metadata
            operational += await self._purge_outbox(session, outbox_cutoff)
            checkpoint_rows = await self._purge_checkpoint_state(session, checkpoint_cutoff)
            return purged, int(result.rowcount or 0), operational, checkpoint_rows

    async def _purge_outbox(self, session: Any, cutoff: datetime) -> int:
        result = await session.execute(
            delete(OutboxRow).where(
                OutboxRow.published_at.is_not(None),
                OutboxRow.published_at <= cutoff,
            )
        )
        parked = await session.execute(
            delete(OutboxRow).where(
                OutboxRow.dead_lettered_at.is_not(None),
                OutboxRow.dead_lettered_at <= cutoff,
            )
        )
        return int(result.rowcount or 0) + int(parked.rowcount or 0)

    async def _purge_checkpoint_state(self, session: Any, cutoff: datetime) -> int:
        """Delete LangGraph checkpoint chains and workflow node boundaries of
        terminal runs past the retention window. Graph execution rows survive
        as compact audit metadata; only the blob state is reclaimed."""
        run_ids = tuple(
            (
                await session.scalars(
                    select(RunRow.id).where(
                        RunRow.state.in_(TERMINAL_RUN_STATES),
                        RunRow.created_at <= cutoff,
                    )
                )
            ).all()
        )
        if not run_ids:
            return 0
        thread_ids = tuple(
            (
                await session.scalars(
                    select(GraphExecutionRow.thread_id)
                    .join(TaskRow, TaskRow.id == GraphExecutionRow.task_id)
                    .where(TaskRow.run_id.in_(run_ids))
                )
            ).all()
        )
        deleted = 0
        if thread_ids:
            for table in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"):
                result = await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = ANY(:thread_ids)"),  # noqa: S608 - fixed literal allowlist
                    {"thread_ids": list(thread_ids)},
                )
                deleted += int(result.rowcount or 0)
        attempt_ids = tuple(
            (
                await session.scalars(
                    select(TaskAttemptRow.id)
                    .join(TaskRow, TaskRow.id == TaskAttemptRow.task_id)
                    .where(TaskRow.run_id.in_(run_ids))
                )
            ).all()
        )
        if attempt_ids:
            result = await session.execute(
                text(
                    "DELETE FROM workflow_node_executions "
                    "WHERE attempt_id = ANY(:attempt_ids)"
                ),
                {"attempt_ids": list(attempt_ids)},
            )
            deleted += int(result.rowcount or 0)
        return deleted

    async def _purge_operational_metadata(self, session: Any, cutoff: datetime) -> int:
        deleted = 0
        approvals = tuple(
            (
                await session.scalars(
                    select(ApprovalRow).where(
                        ApprovalRow.created_at <= cutoff,
                        ApprovalRow.status != ApprovalStatus.PENDING,
                    )
                )
            ).all()
        )
        for approval in approvals:
            tool = await session.get(ToolExecutionRow, approval.call_id)
            if tool is None or not await self._run_is_terminal(session, tool.run_id):
                continue
            if await self._has_audit_reference(
                session, aggregate_type="approval", aggregate_id=approval.id
            ):
                continue
            await session.delete(approval)
            deleted += 1
        await session.flush()

        tools = tuple(
            (
                await session.scalars(
                    select(ToolExecutionRow).where(ToolExecutionRow.created_at <= cutoff)
                )
            ).all()
        )
        for tool in tools:
            has_approval = await session.scalar(
                select(exists().where(ApprovalRow.call_id == tool.id))
            )
            if has_approval or not await self._run_is_terminal(session, tool.run_id):
                continue
            if await self._has_audit_reference(
                session, aggregate_type="tool_execution", aggregate_id=tool.id
            ):
                continue
            await session.delete(tool)
            deleted += 1
        await session.flush()

        audits = tuple(
            (
                await session.scalars(
                    select(AuditEventRow).where(AuditEventRow.created_at <= cutoff)
                )
            ).all()
        )
        for audit in audits:
            if not await self._run_is_terminal(session, audit.correlation_id):
                continue
            if audit.aggregate_type == "approval":
                approval = await session.get(ApprovalRow, audit.aggregate_id)
                if approval is not None and approval.status == ApprovalStatus.PENDING:
                    continue
            if audit.aggregate_type == "memory_candidate":
                candidate = await session.get(MemoryCandidateRow, audit.aggregate_id)
                if candidate is not None and candidate.status in CURRENT_PROMOTION_STATES:
                    continue
            await session.delete(audit)
            deleted += 1
        await session.flush()
        return deleted

    async def _run_is_terminal(self, session: Any, run_id: Any) -> bool:
        run = await session.get(RunRow, run_id)
        return bool(run is None or run.state in TERMINAL_RUN_STATES)

    async def _has_audit_reference(
        self, session: Any, *, aggregate_type: str, aggregate_id: Any
    ) -> bool:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        AuditEventRow.aggregate_type == aggregate_type,
                        AuditEventRow.aggregate_id == aggregate_id,
                    )
                )
            )
        )

    async def _message_is_protected(self, session: Any, message: AgentMessageRow) -> bool:
        audit_ref = await session.scalar(
            select(
                exists().where(
                    (AuditEventRow.aggregate_type == "message")
                    & (AuditEventRow.aggregate_id == message.id)
                    | (AuditEventRow.payload["message_id"].as_string() == str(message.id))
                )
            )
        )
        if audit_ref:
            return True
        promotion_ref = await session.scalar(
            select(
                exists().where(
                    MemoryCandidateRow.status.in_(CURRENT_PROMOTION_STATES),
                    MemoryCandidateRow.candidate["source_message_id"].as_string()
                    == str(message.id),
                )
            )
        )
        if promotion_ref:
            return True
        approval_ref = await session.scalar(
            select(
                exists(
                    select(ApprovalRow.id)
                    .join(ToolExecutionRow, ToolExecutionRow.id == ApprovalRow.call_id)
                    .where(
                        ToolExecutionRow.task_id == message.task_id,
                        ApprovalRow.status == ApprovalStatus.PENDING,
                    )
                )
            )
        )
        return bool(approval_ref)
