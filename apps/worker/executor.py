from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver

from apps.dispatcher.main import DispatchMessage
from apps.worker.runner import WorkerOutcome
from domain.enums import GraphExecutionState, RiskLevel, TaskStatus, TaskType
from execution.scheduler.service import SchedulerService, TaskExecutionLease
from observability.tracing import CorrelationContext, bind_correlation, reset_correlation
from persistence.database import Database
from workflows.runtime import CheckpointedWorkflowRuntime
from workflows.state import CheckpointIdentity, TaskExecutionInput
from workflows.task_subgraphs import TaskNodeExecutor, build_task_subgraph

NodeExecutorFactory = Callable[["TaskExecutionContext"], Awaitable[TaskNodeExecutor]]
CheckpointerFactory = Callable[
    [], AbstractAsyncContextManager[BaseCheckpointSaver[Any] | None]
]


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    task_id: UUID
    attempt_id: UUID
    baseline_commit: str
    source_path: str
    task_type: TaskType
    goal: str
    allowed_tools: tuple[str, ...]
    assigned_capability: str
    risk_ceiling: RiskLevel
    dependencies: tuple[UUID, ...]

    @classmethod
    def from_lease(cls, lease: TaskExecutionLease) -> TaskExecutionContext:
        return cls(
            run_id=lease.run_id,
            project_id=lease.project_id,
            repository_id=lease.repository_id,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            baseline_commit=lease.baseline_commit,
            source_path=lease.source_path,
            task_type=lease.task_type,
            goal=lease.goal,
            allowed_tools=lease.allowed_tools,
            assigned_capability=lease.assigned_capability,
            risk_ceiling=lease.risk_ceiling,
            dependencies=lease.dependencies,
        )


class DispatchedTaskExecutor:
    """Bind exact scheduler leases to durable typed LangGraph task execution."""

    def __init__(
        self,
        *,
        database: Database,
        scheduler: SchedulerService,
        node_executor_factory: NodeExecutorFactory,
        checkpointer_factory: CheckpointerFactory,
        production_graph: bool,
        agent_spec_hash: str,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        if len(agent_spec_hash) != 64 or heartbeat_seconds <= 0:
            raise ValueError("agent spec hash and heartbeat interval must be valid")
        self._database = database
        self._scheduler = scheduler
        self._node_executor_factory = node_executor_factory
        self._checkpointer_factory = checkpointer_factory
        self._production_graph = production_graph
        self._agent_spec_hash = agent_spec_hash
        self._heartbeat_seconds = heartbeat_seconds

    async def execute(self, message: DispatchMessage) -> WorkerOutcome:
        lease = await self._scheduler.start_claim(
            task_id=message.task_id,
            project_id=message.project_id,
            owner=message.owner,
            token=message.lease_token,
            attempt_id=message.attempt_id,
            agent_spec_hash=self._agent_spec_hash,
        )
        if lease.already_terminal:
            return WorkerOutcome.COMPLETED
        context = TaskExecutionContext.from_lease(lease)
        node_executor = await self._node_executor_factory(context)
        correlation_token = bind_correlation(
            CorrelationContext(
                trace_id=f"task:{context.task_id}:attempt:{context.attempt_id}",
                run_id=context.run_id,
                task_id=context.task_id,
                graph_thread_id=f"run:{context.run_id}:task:{context.task_id}",
            )
        )
        current_execution_task = asyncio.current_task()
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(message, heartbeat_stop, execution_task=current_execution_task)
        )
        try:
            async with self._checkpointer_factory() as checkpointer:
                graph = build_task_subgraph(
                    context.task_type,
                    executor=node_executor,
                    checkpointer=checkpointer,
                    production=self._production_graph,
                )
                runtime = CheckpointedWorkflowRuntime(
                    database=self._database,
                    graph=cast(Any, graph),
                )
                outcome = await runtime.invoke(
                    CheckpointIdentity(
                        run_id=context.run_id,
                        task_id=context.task_id,
                        attempt_id=context.attempt_id,
                        project_id=context.project_id,
                        repository_id=context.repository_id,
                        baseline_commit=context.baseline_commit,
                    ),
                    cast(
                        dict[str, Any],
                        TaskExecutionInput(
                            trace_id=f"task:{context.task_id}:attempt:{context.attempt_id}",
                            run_id=context.run_id,
                            project_id=context.project_id,
                            repository_id=context.repository_id,
                            task_id=context.task_id,
                            attempt_id=context.attempt_id,
                            baseline_commit=context.baseline_commit,
                            task_type=context.task_type,
                            goal=context.goal,
                        ).to_state(),
                    ),
                )
            if outcome.state is GraphExecutionState.COMPLETED:
                await self._scheduler.finish_claim(
                    task_id=context.task_id,
                    project_id=context.project_id,
                    owner=message.owner,
                    token=message.lease_token,
                    attempt_id=context.attempt_id,
                    target=TaskStatus.COMPLETED,
                )
                return WorkerOutcome.COMPLETED
            return WorkerOutcome.WAITING
        except asyncio.CancelledError:
            if not heartbeat_stop.is_set():
                heartbeat_stop.set()
                heartbeat.cancel()
            try:
                await self._scheduler.finish_claim(
                    task_id=context.task_id,
                    project_id=context.project_id,
                    owner=message.owner,
                    token=message.lease_token,
                    attempt_id=context.attempt_id,
                    target=TaskStatus.FAILED,
                )
            except (PermissionError, LookupError):
                pass
            raise
        except Exception:
            await self._scheduler.finish_claim(
                task_id=context.task_id,
                project_id=context.project_id,
                owner=message.owner,
                token=message.lease_token,
                attempt_id=context.attempt_id,
                target=TaskStatus.FAILED,
            )
            raise
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, PermissionError):
                pass
            reset_correlation(correlation_token)

    async def _heartbeat(
        self,
        message: DispatchMessage,
        stop: asyncio.Event,
        execution_task: asyncio.Task[Any] | None = None,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
            except TimeoutError:
                if not await self._scheduler.heartbeat(
                    task_id=message.task_id,
                    owner=message.owner,
                    token=message.lease_token,
                ):
                    if execution_task is not None and not execution_task.done():
                        execution_task.cancel()
                    raise PermissionError("worker lost its scheduler lease") from None
