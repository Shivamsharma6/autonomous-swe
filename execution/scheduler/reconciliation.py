from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import GraphExecutionState, TaskStatus
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    GraphExecutionRow,
    LeaseRow,
    ReservationRow,
    RunRow,
    TaskRow,
)


class ReconciliationAction(StrEnum):
    NOOP = "NOOP"
    RESUME_CHECKPOINT = "RESUME_CHECKPOINT"
    FINALIZE_DOMAIN = "FINALIZE_DOMAIN"
    REQUEUE_MISSING_CHECKPOINT = "REQUEUE_MISSING_CHECKPOINT"
    DOMAIN_TERMINAL_WINS = "DOMAIN_TERMINAL_WINS"
    QUARANTINE = "QUARANTINE"


class ReconciliationService:
    def __init__(self, *, database: Database, repository: DomainRepository | None = None) -> None:
        self.database = database
        self.repository = repository or DomainRepository()

    async def reconcile(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        now: datetime | None = None,
    ) -> ReconciliationAction:
        current_time = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            task = await session.scalar(
                select(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.project_id == project_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"task {task_id} does not exist in project {project_id}")
            run = await session.get(RunRow, task.run_id)
            graph = await session.scalar(
                select(GraphExecutionRow)
                .where(GraphExecutionRow.task_id == task_id)
                .with_for_update()
            )
            lease = await session.scalar(
                select(LeaseRow).where(LeaseRow.task_id == task_id).with_for_update()
            )

            action = self._decide(task, run, graph, lease, current_time)
            if action is ReconciliationAction.NOOP:
                return action

            if action is ReconciliationAction.RESUME_CHECKPOINT:
                await self._set_task_state(task, TaskStatus.READY)
                await self._release_lease(session, task.id, current_time)
            elif action is ReconciliationAction.FINALIZE_DOMAIN:
                await self._set_task_state(task, TaskStatus.COMPLETED)
                await self._release_lease(session, task.id, current_time)
            elif action is ReconciliationAction.REQUEUE_MISSING_CHECKPOINT:
                await self._set_task_state(task, TaskStatus.READY)
                await self._release_lease(session, task.id, current_time)
            elif action is ReconciliationAction.DOMAIN_TERMINAL_WINS:
                if graph is not None:
                    graph.state = GraphExecutionState.CANCELLED
                    graph.state_entered_at = current_time
                await self._release_lease(session, task.id, current_time)
            elif action is ReconciliationAction.QUARANTINE:
                if task.state not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    await self._set_task_state(task, TaskStatus.BLOCKED)
                if graph is not None:
                    graph.state = GraphExecutionState.NEEDS_RECONCILIATION
                    graph.state_entered_at = current_time
                await self._release_lease(session, task.id, current_time)

            event_id = uuid4()
            payload = {
                "action": action.value,
                "task_id": str(task.id),
                "domain_state": task.state.value,
                "graph_state": graph.state.value if graph else None,
            }
            await self.repository.append_audit(
                session,
                event_id=event_id,
                event_type=f"reconciliation.{action.value.lower()}",
                aggregate_type="task",
                aggregate_id=task.id,
                payload=payload,
                correlation_id=task.run_id,
                causation_id=event_id,
            )
            await self.repository.enqueue_event(
                session,
                event_id=event_id,
                topic="reconciliation",
                payload=payload,
            )
            await session.flush()
            return action

    @staticmethod
    def _decide(
        task: TaskRow,
        run: RunRow | None,
        graph: GraphExecutionRow | None,
        lease: LeaseRow | None,
        now: datetime,
    ) -> ReconciliationAction:
        terminal_tasks = {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        terminal_graphs = {
            GraphExecutionState.COMPLETED,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
        }
        if graph is not None and graph.state is GraphExecutionState.NEEDS_RECONCILIATION:
            return ReconciliationAction.NOOP
        if graph is not None and (
            run is None
            or graph.run_id != task.run_id
            or graph.repository_id != task.repository_id
            or graph.baseline_commit != run.baseline_commit
        ):
            return ReconciliationAction.QUARANTINE
        if task.state in terminal_tasks:
            if graph is None or graph.state is GraphExecutionState.CANCELLED:
                return ReconciliationAction.NOOP
            if graph.state in terminal_graphs:
                if (
                    task.state is TaskStatus.COMPLETED
                    and graph.state is GraphExecutionState.COMPLETED
                ) or (
                    task.state is TaskStatus.FAILED and graph.state is GraphExecutionState.FAILED
                ):
                    return ReconciliationAction.NOOP
                return ReconciliationAction.QUARANTINE
            return ReconciliationAction.DOMAIN_TERMINAL_WINS
        if task.state is TaskStatus.RUNNING:
            if graph is None:
                return ReconciliationAction.REQUEUE_MISSING_CHECKPOINT
            if graph.state is GraphExecutionState.COMPLETED:
                return ReconciliationAction.FINALIZE_DOMAIN
            if lease is not None and lease.expires_at <= now:
                return ReconciliationAction.RESUME_CHECKPOINT
        return ReconciliationAction.NOOP

    @staticmethod
    async def _set_task_state(task: TaskRow, target: TaskStatus) -> None:
        task.state = target
        task.version += 1
        task.state_entered_at = datetime.now(UTC)

    @staticmethod
    async def _release_lease(session: AsyncSession, task_id: UUID, now: datetime) -> None:
        await session.execute(
            update(ReservationRow)
            .where(
                ReservationRow.task_id == task_id,
                ReservationRow.released_at.is_(None),
            )
            .values(released_at=now)
        )
        await session.execute(delete(LeaseRow).where(LeaseRow.task_id == task_id))
