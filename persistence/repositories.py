from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import ArtifactState, GraphExecutionState, TaskStatus
from domain.events import require_task_transition
from domain.messages import MessageEnvelope
from domain.models import (
    ApprovalRequest,
    ArtifactRef,
    MemoryCandidate,
    SandboxExecution,
    TaskSpec,
    ToolCallRequest,
    canonical_sha256,
)
from persistence.tables import (
    AgentMessageRow,
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    ConsumerDeliveryRow,
    ConsumerReceiptRow,
    DeadLetterRow,
    GraphExecutionRow,
    LeaseRow,
    MemoryCandidateRow,
    ModelCallRow,
    OutboxRow,
    PlanRevisionRow,
    ProjectRow,
    ProjectTaskResourceEstimateRow,
    RepositoryRow,
    ReservationRow,
    RunRow,
    SandboxExecutionRow,
    StateDurationRow,
    TaskAttemptRow,
    TaskRow,
    ToolExecutionRow,
    utc_now,
)


class StaleStateError(RuntimeError):
    pass


class DomainRepository:
    async def create_project(
        self, session: AsyncSession, *, project_id: UUID, name: str
    ) -> ProjectRow:
        row = ProjectRow(id=project_id, name=name)
        session.add(row)
        await session.flush()
        return row

    async def create_repository(
        self,
        session: AsyncSession,
        *,
        repository_id: UUID,
        project_id: UUID,
        source_path: str,
        default_branch: str,
    ) -> RepositoryRow:
        row = RepositoryRow(
            id=repository_id,
            project_id=project_id,
            source_path=source_path,
            default_branch=default_branch,
        )
        session.add(row)
        await session.flush()
        return row

    async def create_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        project_id: UUID,
        repository_id: UUID,
        goal: str,
        baseline_commit: str,
    ) -> RunRow:
        row = RunRow(
            id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal=goal,
            baseline_commit=baseline_commit,
        )
        session.add(row)
        await session.flush()
        return row

    async def create_plan_revision(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        revision: int,
        plan: dict[str, Any],
    ) -> PlanRevisionRow:
        row = PlanRevisionRow(run_id=run_id, revision=revision, plan=plan)
        session.add(row)
        await session.flush()
        return row

    async def create_task(self, session: AsyncSession, *, run_id: UUID, task: TaskSpec) -> TaskRow:
        row = TaskRow(
            id=task.id,
            run_id=run_id,
            project_id=task.project_id,
            repository_id=task.repository_id,
            plan_revision=task.plan_revision,
            title=task.title,
            description=task.description,
            task_type=task.task_type,
            dependencies=[str(value) for value in task.dependencies],
            priority=task.priority,
            assigned_capability=task.assigned_capability,
            acceptance_criteria=list(task.acceptance_criteria),
            allowed_tools=list(task.allowed_tools),
            risk_ceiling=task.risk_ceiling.value,
            expected_artifacts=list(task.expected_artifacts),
            repository_paths=list(task.repository_paths),
            retry_policy=task.retry_policy.model_dump(mode="json"),
            budget=task.budget.model_dump(mode="json"),
            estimate=task.estimate.model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
        return row

    async def get_task(
        self, session: AsyncSession, *, project_id: UUID, task_id: UUID
    ) -> TaskRow | None:
        return cast(
            TaskRow | None,
            await session.scalar(
                select(TaskRow).where(TaskRow.id == task_id, TaskRow.project_id == project_id)
            ),
        )

    async def transition_task(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        task_id: UUID,
        expected_version: int,
        target: TaskStatus,
    ) -> TaskRow:
        row = await session.scalar(
            select(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.project_id == project_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError(f"task {task_id} does not exist in project {project_id}")
        if row.version != expected_version:
            raise StaleStateError(
                f"task {task_id} expected version {expected_version}, found {row.version}"
            )
        require_task_transition(row.state, target)
        previous = row.state
        row.state = target
        row.version += 1
        row.state_entered_at = utc_now()
        event_id = uuid4()
        payload = {
            "task_id": str(task_id),
            "project_id": str(project_id),
            "from": previous.value,
            "to": target.value,
            "version": row.version,
        }
        await self.append_audit(
            session,
            event_id=event_id,
            event_type="task.state_changed",
            aggregate_type="task",
            aggregate_id=task_id,
            payload=payload,
            correlation_id=row.run_id,
            causation_id=event_id,
        )
        await self.enqueue_event(
            session,
            event_id=event_id,
            topic="task-state",
            payload=payload,
        )
        await session.flush()
        return row

    async def create_attempt(
        self,
        session: AsyncSession,
        *,
        attempt_id: UUID,
        task_id: UUID,
        agent_spec_hash: str,
    ) -> TaskAttemptRow:
        row = TaskAttemptRow(
            id=attempt_id,
            task_id=task_id,
            agent_spec_hash=agent_spec_hash,
        )
        session.add(row)
        await session.flush()
        return row

    async def create_lease(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        owner: str,
        token: UUID,
        expires_at: datetime,
    ) -> LeaseRow:
        row = LeaseRow(task_id=task_id, owner=owner, token=token, expires_at=expires_at)
        session.add(row)
        await session.flush()
        return row

    async def create_reservation(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        project_id: UUID,
        resource: str,
        units: int,
    ) -> ReservationRow:
        row = ReservationRow(
            task_id=task_id,
            project_id=project_id,
            resource=resource,
            units=units,
        )
        session.add(row)
        await session.flush()
        return row

    async def record_graph_execution(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        run_id: UUID,
        repository_id: UUID,
        baseline_commit: str,
        thread_id: str,
        state: GraphExecutionState,
        checkpoint_id: str | None,
    ) -> GraphExecutionRow:
        row = GraphExecutionRow(
            task_id=task_id,
            run_id=run_id,
            repository_id=repository_id,
            baseline_commit=baseline_commit,
            thread_id=thread_id,
            state=state,
            checkpoint_id=checkpoint_id,
        )
        session.add(row)
        await session.flush()
        return row

    async def persist_message(
        self, session: AsyncSession, message: MessageEnvelope
    ) -> AgentMessageRow:
        row = AgentMessageRow(
            id=message.message_id,
            run_id=message.run_id,
            task_id=message.task_id,
            attempt_id=message.attempt_id,
            kind=message.kind.value,
            schema_version=message.schema_version,
            sender=message.sender,
            recipient=message.recipient,
            causation_id=message.causation_id,
            correlation_id=message.correlation_id,
            artifact_ids=[str(value) for value in message.artifact_ids],
            payload=message.model_dump(mode="json"),
            content_hash=message.content_hash,
            created_at=message.created_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def enqueue_event(
        self,
        session: AsyncSession,
        *,
        event_id: UUID,
        topic: str,
        payload: dict[str, Any],
    ) -> OutboxRow:
        row = OutboxRow(event_id=event_id, topic=topic, payload=payload)
        session.add(row)
        await session.flush()
        return row

    async def claim_consumer_receipt(
        self, session: AsyncSession, *, consumer: str, event_id: UUID
    ) -> bool:
        statement = (
            insert(ConsumerReceiptRow)
            .values(consumer=consumer, event_id=event_id)
            .on_conflict_do_nothing(constraint="uq_consumer_event_receipt")
        )
        result = cast(CursorResult[Any], await session.execute(statement))
        return bool(result.rowcount)

    async def record_dead_letter(
        self,
        session: AsyncSession,
        *,
        event_id: UUID,
        topic: str,
        payload: dict[str, Any],
        attempts: int,
        last_error: str,
        causation_chain: tuple[UUID, ...],
        consumer: str = "legacy",
    ) -> DeadLetterRow:
        row = DeadLetterRow(
            event_id=event_id,
            consumer=consumer,
            topic=topic,
            payload=payload,
            attempts=attempts,
            last_error=last_error,
            causation_chain=[str(value) for value in causation_chain],
        )
        session.add(row)
        await session.flush()
        return row

    async def record_tool_execution(
        self,
        session: AsyncSession,
        *,
        call: ToolCallRequest,
        status: str,
        result: dict[str, Any],
    ) -> ToolExecutionRow:
        row = ToolExecutionRow(
            id=call.call_id,
            run_id=call.run_id,
            task_id=call.task_id,
            attempt_id=call.attempt_id,
            requested_by=call.requested_by,
            tool_name=call.tool_name,
            arguments=call.arguments,
            idempotency_key=call.idempotency_key,
            status=status,
            result=result,
            completed_at=utc_now() if status == "COMPLETED" else None,
        )
        session.add(row)
        await session.flush()
        return row

    async def create_approval(
        self, session: AsyncSession, approval: ApprovalRequest
    ) -> ApprovalRow:
        row = ApprovalRow(
            id=approval.approval_id,
            call_id=approval.call.call_id,
            project_id=approval.project_id,
            repository_id=approval.repository_id,
            baseline_commit=approval.baseline_commit,
            call_hash=approval.call_hash,
            status=approval.status,
            expires_at=approval.expires_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def record_artifact(
        self,
        session: AsyncSession,
        *,
        artifact: ArtifactRef,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID,
        storage_key: str,
        size_bytes: int,
    ) -> ArtifactRow:
        row = ArtifactRow(
            id=artifact.artifact_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            sha256=artifact.sha256,
            media_type=artifact.media_type,
            state=artifact.state,
            storage_key=storage_key,
            size_bytes=size_bytes,
            verified_at=utc_now() if artifact.state.value == "VALID" else None,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_artifact(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        artifact_id: UUID,
        for_update: bool = False,
    ) -> ArtifactRow | None:
        statement = select(ArtifactRow).where(
            ArtifactRow.id == artifact_id,
            ArtifactRow.project_id == project_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ArtifactRow | None, await session.scalar(statement))

    async def list_valid_artifacts(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        task_id: UUID,
    ) -> tuple[ArtifactRow, ...]:
        result = await session.scalars(
            select(ArtifactRow)
            .where(
                ArtifactRow.project_id == project_id,
                ArtifactRow.task_id == task_id,
                ArtifactRow.state == ArtifactState.VALID,
            )
            .order_by(ArtifactRow.created_at, ArtifactRow.id)
        )
        return tuple(result)

    async def mark_artifact_corrupt(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactRow:
        row = await self.get_artifact(
            session,
            project_id=project_id,
            artifact_id=artifact_id,
            for_update=True,
        )
        if row is None:
            raise LookupError(f"artifact {artifact_id} does not exist in project {project_id}")
        if row.state is ArtifactState.CORRUPT:
            return row
        row.state = ArtifactState.CORRUPT
        row.verified_at = None
        event_id = uuid4()
        payload = {
            "artifact_id": str(row.id),
            "project_id": str(row.project_id),
            "sha256": row.sha256,
            "state": ArtifactState.CORRUPT.value,
        }
        await self.append_audit(
            session,
            event_id=event_id,
            event_type="artifact.corrupt",
            aggregate_type="artifact",
            aggregate_id=row.id,
            payload=payload,
            correlation_id=row.run_id,
            causation_id=event_id,
        )
        await self.enqueue_event(
            session,
            event_id=event_id,
            topic="artifact-integrity",
            payload=payload,
        )
        await session.flush()
        return row

    async def create_memory_candidate(
        self, session: AsyncSession, candidate: MemoryCandidate
    ) -> MemoryCandidateRow:
        row = MemoryCandidateRow(
            id=candidate.candidate_id,
            project_id=candidate.project_id,
            run_id=candidate.source_run_id,
            task_id=candidate.source_task_id,
            attempt_id=candidate.source_attempt_id,
            classification=candidate.classification,
            content=candidate.content,
            candidate=candidate.model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
        return row

    async def record_sandbox_execution(
        self,
        session: AsyncSession,
        execution: SandboxExecution,
        *,
        attempt_id: UUID,
    ) -> SandboxExecutionRow:
        values = execution.model_dump(exclude={"schema_version", "execution_id", "task_id"})
        row = SandboxExecutionRow(
            id=execution.execution_id,
            task_id=execution.task_id,
            attempt_id=attempt_id,
            **values,
        )
        session.add(row)
        await session.flush()
        return row

    async def record_state_duration(
        self,
        session: AsyncSession,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        state: str,
        entered_at: datetime,
        exited_at: datetime,
    ) -> StateDurationRow:
        row = StateDurationRow(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            state=state,
            entered_at=entered_at,
            exited_at=exited_at,
            duration_seconds=max(0.0, (exited_at - entered_at).total_seconds()),
        )
        session.add(row)
        await session.flush()
        return row

    async def append_audit(
        self,
        session: AsyncSession,
        *,
        event_id: UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        correlation_id: UUID,
        causation_id: UUID,
    ) -> AuditEventRow:
        content_hash = canonical_sha256(
            {
                "event_id": str(event_id),
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": str(aggregate_id),
                "payload": payload,
                "correlation_id": str(correlation_id),
                "causation_id": str(causation_id),
            }
        )
        row = AuditEventRow(
            id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            content_hash=content_hash,
        )
        session.add(row)
        await session.flush()
        return row

    async def record_counts(self, session: AsyncSession) -> dict[str, int]:
        rows = (
            ProjectRow,
            RepositoryRow,
            RunRow,
            PlanRevisionRow,
            TaskRow,
            TaskAttemptRow,
            ModelCallRow,
            LeaseRow,
            ReservationRow,
            GraphExecutionRow,
            ProjectTaskResourceEstimateRow,
            AgentMessageRow,
            OutboxRow,
            ConsumerReceiptRow,
            ConsumerDeliveryRow,
            DeadLetterRow,
            ToolExecutionRow,
            ApprovalRow,
            ArtifactRow,
            MemoryCandidateRow,
            SandboxExecutionRow,
            StateDurationRow,
            AuditEventRow,
        )
        counts: dict[str, int] = {}
        for row in rows:
            value = await session.scalar(select(func.count()).select_from(row))
            counts[row.__tablename__] = int(value or 0)
        return counts
