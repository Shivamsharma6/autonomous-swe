from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langgraph.types import Command
from sqlalchemy import select

from domain.enums import GraphExecutionState, TaskStatus
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import RunRow, TaskAttemptRow, TaskRow
from workflows.state import WAIT_GRAPH_STATES, CheckpointIdentity, WaitKind


class CheckpointedGraph(Protocol):
    async def ainvoke(
        self,
        input: dict[str, Any] | Command[Any],
        config: dict[str, Any],
        *,
        durability: str,
    ) -> dict[str, Any]: ...

    async def aget_state(self, config: dict[str, Any]) -> Any: ...


class CheckpointIdentityError(RuntimeError):
    pass


class WorkflowCancellation(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckpointOutcome:
    state: GraphExecutionState
    checkpoint_id: str | None
    values: dict[str, Any]
    interrupt: dict[str, Any] | None


class CheckpointedWorkflowRuntime:
    def __init__(
        self,
        *,
        database: Database,
        graph: CheckpointedGraph,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._graph = graph
        self._repository = repository or DomainRepository()

    async def invoke(
        self,
        identity: CheckpointIdentity,
        graph_input: dict[str, Any] | Command[Any],
    ) -> CheckpointOutcome:
        await self._assert_active(identity)
        await self._persist_state(identity, GraphExecutionState.RUNNING, checkpoint_id=None)
        config = {"configurable": {"thread_id": identity.thread_id}}
        try:
            values = await self._graph.ainvoke(
                graph_input,
                config,
                durability="sync",
            )
            snapshot = await self._graph.aget_state(config)
            checkpoint_id = cast(
                str | None,
                snapshot.config.get("configurable", {}).get("checkpoint_id"),
            )
            interrupt_payload = _interrupt_payload(values)
            if interrupt_payload is not None:
                wait_kind = WaitKind(str(interrupt_payload["kind"]))
                graph_state = WAIT_GRAPH_STATES[wait_kind]
            elif snapshot.next:
                graph_state = GraphExecutionState.PAUSED
            else:
                graph_state = GraphExecutionState.COMPLETED
            await self._assert_active(identity)
            await self._persist_state(identity, graph_state, checkpoint_id=checkpoint_id)
            return CheckpointOutcome(
                state=graph_state,
                checkpoint_id=checkpoint_id,
                values=values,
                interrupt=interrupt_payload,
            )
        except (asyncio.CancelledError, WorkflowCancellation):
            await self._persist_state(identity, GraphExecutionState.CANCELLED, checkpoint_id=None)
            raise
        except Exception:
            await self._persist_state(identity, GraphExecutionState.FAILED, checkpoint_id=None)
            raise

    async def _assert_active(self, identity: CheckpointIdentity) -> None:
        async with self._database.transaction() as session:
            task = await session.scalar(
                select(TaskRow).where(
                    TaskRow.id == identity.task_id,
                    TaskRow.project_id == identity.project_id,
                )
            )
            run = await session.get(RunRow, identity.run_id)
            attempt = await session.get(TaskAttemptRow, identity.attempt_id)
            if task is None or run is None or attempt is None:
                raise CheckpointIdentityError("workflow task or run does not exist")
            if (
                task.run_id != identity.run_id
                or attempt.task_id != identity.task_id
                or task.repository_id != identity.repository_id
                or run.repository_id != identity.repository_id
                or run.baseline_commit != identity.baseline_commit
            ):
                raise CheckpointIdentityError(
                    "workflow identity does not match run, task, repository, or baseline"
                )
            if task.state is TaskStatus.CANCELLED or run.cancellation_requested_at is not None:
                raise WorkflowCancellation(f"workflow task {identity.task_id} is cancelled")
            if task.state is not TaskStatus.RUNNING:
                raise CheckpointIdentityError(
                    f"scheduler task {identity.task_id} is {task.state.value}, not RUNNING"
                )

    async def _persist_state(
        self,
        identity: CheckpointIdentity,
        state: GraphExecutionState,
        *,
        checkpoint_id: str | None,
    ) -> None:
        async with self._database.transaction() as session:
            await self._repository.transition_graph_execution(
                session,
                task_id=identity.task_id,
                run_id=identity.run_id,
                repository_id=identity.repository_id,
                baseline_commit=identity.baseline_commit,
                thread_id=identity.thread_id,
                target=state,
                checkpoint_id=checkpoint_id,
            )


def _interrupt_payload(values: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = values.get("__interrupt__")
    if not isinstance(interrupts, list) or not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    if not isinstance(value, dict):
        raise ValueError("workflow interrupt must contain an object payload")
    return cast(dict[str, Any], value)
