from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from domain.enums import TaskType
from workflows.state import (
    NodeExecutionRequest,
    NodeExecutionResult,
    TaskGraphResult,
    TaskWorkflowState,
    WaitKind,
    WaitWorkflowState,
)

TASK_NODE_SEQUENCES: dict[TaskType, tuple[str, ...]] = {
    TaskType.RESEARCH: ("recall", "investigate", "evidence", "synthesis"),
    TaskType.IMPLEMENTATION: ("recall", "implement", "targeted_test", "review"),
    TaskType.TEST: ("recall", "generate_tests", "execute", "review"),
    TaskType.REFACTOR: (
        "recall",
        "establish_invariants",
        "refactor",
        "regression_verify",
        "review",
    ),
    TaskType.DOCUMENTATION: ("recall", "draft", "validate_examples", "review"),
    TaskType.VALIDATION: ("recall", "inspect", "verify", "evidence"),
}


class TaskNodeExecutor(Protocol):
    """Claims each NodeExecutionRequest.idempotency_key before external side effects."""

    async def cancellation_requested(self, task_id: UUID) -> bool: ...

    async def execute(self, request: NodeExecutionRequest) -> NodeExecutionResult: ...


class WorkflowCancelled(RuntimeError):
    pass


def build_task_subgraph(
    task_type: TaskType,
    *,
    executor: TaskNodeExecutor,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    production: bool = False,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    _require_production_saver(checkpointer, production=production)
    sequence = TASK_NODE_SEQUENCES[task_type]
    builder = StateGraph(TaskWorkflowState)
    for node_name in sequence:
        builder.add_node(
            node_name,
            cast(Any, _task_node(node_name, task_type, executor)),
        )
    builder.add_edge(START, sequence[0])
    for current, following in zip(sequence, sequence[1:], strict=False):
        builder.add_edge(current, following)
    builder.add_edge(sequence[-1], END)
    return builder.compile(checkpointer=checkpointer, name=f"{task_type.value.casefold()}-task")


def _task_node(
    node_name: str,
    task_type: TaskType,
    executor: TaskNodeExecutor,
) -> Callable[[TaskWorkflowState], Awaitable[dict[str, Any]]]:
    async def execute_node(state: TaskWorkflowState) -> dict[str, Any]:
        if TaskType(state["task_type"]) is not task_type:
            raise ValueError(
                f"task state type {state['task_type']} cannot run {task_type.value} subgraph"
            )
        request = NodeExecutionRequest.from_state(state, node_name)
        if await executor.cancellation_requested(request.task_id):
            raise WorkflowCancelled(f"task {request.task_id} was cancelled before {node_name}")
        result = await executor.execute(request)
        if await executor.cancellation_requested(request.task_id):
            raise WorkflowCancelled(f"task {request.task_id} was cancelled after {node_name}")
        return {
            "completed_nodes": [node_name],
            "message_ids": [str(value) for value in result.message_ids],
            "artifact_ids": [str(value) for value in result.artifact_ids],
            "summaries": {node_name: result.summary},
            "result_id": str(result.result_id),
        }

    return execute_node


def task_result_from_state(state: dict[str, Any]) -> TaskGraphResult:
    return TaskGraphResult(
        task_id=UUID(state["task_id"]),
        attempt_id=UUID(state["attempt_id"]),
        task_type=TaskType(state["task_type"]),
        completed_nodes=tuple(state.get("completed_nodes", ())),
        message_ids=tuple(UUID(value) for value in state.get("message_ids", ())),
        artifact_ids=tuple(UUID(value) for value in state.get("artifact_ids", ())),
        result_id=UUID(state["result_id"]),
        summaries=cast(dict[str, str], state.get("summaries", {})),
    )


def build_wait_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None,
    production: bool = False,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    _require_production_saver(checkpointer, production=production)

    async def await_external(state: WaitWorkflowState) -> dict[str, Any]:
        wait_kind = WaitKind(state["wait_kind"])
        resumed = interrupt(
            {
                "kind": wait_kind.value,
                "request_id": state["request_id"],
                "task_id": state["task_id"],
            }
        )
        if not isinstance(resumed, dict):
            raise ValueError("workflow resume payload must be an object")
        if str(resumed.get("request_id")) != state["request_id"]:
            raise ValueError("workflow resume request_id does not match the interrupt")
        if resumed.get("released") is not True:
            raise ValueError("workflow wait was not explicitly released")
        return {"resume_payload": resumed}

    builder = StateGraph(WaitWorkflowState)
    builder.add_node("await_external", await_external)
    builder.add_edge(START, "await_external")
    builder.add_edge("await_external", END)
    return builder.compile(checkpointer=checkpointer, name="external-wait")


def _require_production_saver(
    checkpointer: BaseCheckpointSaver[Any] | None, *, production: bool
) -> None:
    if production and not isinstance(checkpointer, AsyncPostgresSaver):
        raise TypeError("production LangGraph compilation requires AsyncPostgresSaver")
