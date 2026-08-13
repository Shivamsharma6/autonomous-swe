from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send, interrupt

from workflows.state import (
    AdmittedTask,
    DispatchWorkflowState,
    RunStageRequest,
    RunStageResult,
    RunWorkflowState,
    SchedulerDispatchBatch,
    SchedulerPublishState,
    TaskDispatchResult,
)


class AdmittedTaskRunner(Protocol):
    """Runs a scheduler-admitted task using task_id as the replay boundary."""

    async def execute(self, task: AdmittedTask) -> TaskDispatchResult: ...


class RunStageExecutor(Protocol):
    """Claims each RunStageRequest.idempotency_key before external side effects."""

    async def cancellation_requested(self, run_id: UUID) -> bool: ...

    async def execute(self, request: RunStageRequest) -> RunStageResult: ...


class RunWorkflowCancelled(RuntimeError):
    pass


DispatchPublish = Callable[[dict[str, Any]], Awaitable[None]]


RUN_STAGE_SEQUENCE = (
    "intake",
    "recall",
    "analyze",
    "architect",
    "validate",
    "admit",
    "verify",
    "repair",
    "review",
    "complete",
)

RUN_GRAPH_NODES = (
    "intake",
    "recall",
    "analyze",
    "architect",
    "validate",
    "admit",
    "send",
    "execute_task",
    "fan_in",
    "verify",
    "repair",
    "review",
    "approval",
    "complete",
)


def build_admitted_task_graph(
    runner: AdmittedTaskRunner,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Fan out only the exact batch transactionally admitted by the scheduler."""

    async def dispatch(_: DispatchWorkflowState) -> dict[str, Any]:
        return {}

    def sends(state: DispatchWorkflowState) -> list[Send]:
        return [Send("execute_task", {"task": task}) for task in state["admitted_tasks"]]

    async def execute_task(state: DispatchWorkflowState) -> dict[str, Any]:
        task = AdmittedTask.model_validate(state["task"])
        result = await runner.execute(task)
        return {"results": [result.model_dump(mode="json")]}

    async def fan_in(state: DispatchWorkflowState) -> dict[str, Any]:
        results = [TaskDispatchResult.model_validate(value) for value in state["results"]]
        ordered = sorted(results, key=lambda value: value.task_id)
        return {"ordered_task_ids": [str(value.task_id) for value in ordered]}

    builder = StateGraph(DispatchWorkflowState)
    builder.add_node("dispatch", cast(Any, dispatch))
    builder.add_node("execute_task", cast(Any, execute_task))
    builder.add_node("fan_in", fan_in)
    builder.add_edge(START, "dispatch")
    builder.add_conditional_edges("dispatch", sends, ["execute_task"])
    builder.add_edge("execute_task", "fan_in")
    builder.add_edge("fan_in", END)
    return builder.compile(name="scheduler-admitted-task-fanout")


def build_scheduler_publish_graph(
    publish: DispatchPublish,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Use LangGraph Send fan-out only after the scheduler has issued exact leases."""

    async def dispatch(_: SchedulerPublishState) -> dict[str, Any]:
        return {}

    def sends(state: SchedulerPublishState) -> list[Send] | str:
        messages = state.get("dispatch_messages", ())
        if not messages:
            return "fan_in"
        return [Send("publish", {"dispatch_message": message}) for message in messages]

    async def publish_one(state: SchedulerPublishState) -> dict[str, Any]:
        message = state["dispatch_message"]
        token = str(message["lease_token"])
        await publish(message)
        return {"published_lease_tokens": [token]}

    async def fan_in(state: SchedulerPublishState) -> dict[str, Any]:
        return {
            "ordered_lease_tokens": sorted(state.get("published_lease_tokens", ()))
        }

    builder = StateGraph(SchedulerPublishState)
    builder.add_node("dispatch", cast(Any, dispatch))
    builder.add_node("publish", cast(Any, publish_one))
    builder.add_node("fan_in", fan_in)
    builder.add_edge(START, "dispatch")
    builder.add_conditional_edges("dispatch", sends, ["publish", "fan_in"])
    builder.add_edge("publish", "fan_in")
    builder.add_edge("fan_in", END)
    return builder.compile(name="scheduler-admitted-dispatch-fanout")


def build_run_graph(
    *,
    stage_executor: RunStageExecutor,
    task_runner: AdmittedTaskRunner,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    production: bool = False,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Checkpointable run graph; only the scheduler's admitted batch is fanned out."""
    if production and not isinstance(checkpointer, AsyncPostgresSaver):
        raise TypeError("production LangGraph compilation requires AsyncPostgresSaver")

    builder = StateGraph(RunWorkflowState)
    for stage in RUN_STAGE_SEQUENCE:
        builder.add_node(
            stage,
            cast(Any, _run_stage(stage, stage_executor)),
        )

    async def send(_: RunWorkflowState) -> dict[str, Any]:
        return {}

    def admitted_sends(state: RunWorkflowState) -> list[Send] | str:
        if not state.get("admitted_tasks"):
            return "fan_in"
        return [Send("execute_task", {"task": task}) for task in state["admitted_tasks"]]

    async def execute_task(state: RunWorkflowState) -> dict[str, Any]:
        task = AdmittedTask.model_validate(state["task"])
        result = await task_runner.execute(task)
        return {"task_results": [result.model_dump(mode="json")]}

    async def fan_in(state: RunWorkflowState) -> dict[str, Any]:
        results = [
            TaskDispatchResult.model_validate(value) for value in state.get("task_results", ())
        ]
        ordered = sorted(results, key=lambda value: value.task_id)
        return {"ordered_task_ids": [str(result.task_id) for result in ordered]}

    async def approval(state: RunWorkflowState) -> dict[str, Any]:
        request_id = state.get("approval_request_id")
        if not request_id:
            return {"approval_decision": {"required": False}}
        decision = interrupt(
            {
                "kind": "approval",
                "request_id": request_id,
                "run_id": state["run_id"],
            }
        )
        if not isinstance(decision, dict):
            raise ValueError("approval resume payload must be an object")
        if str(decision.get("request_id")) != request_id:
            raise ValueError("approval decision does not match the exact request")
        if decision.get("approved") is not True:
            raise PermissionError("run approval was not granted")
        return {"approval_decision": decision}

    builder.add_node("send", cast(Any, send))
    builder.add_node("execute_task", cast(Any, execute_task))
    builder.add_node("fan_in", fan_in)
    builder.add_node("approval", approval)
    builder.add_edge(START, "intake")
    for current, following in zip(
        ("intake", "recall", "analyze", "architect", "validate"),
        ("recall", "analyze", "architect", "validate", "admit"),
        strict=True,
    ):
        builder.add_edge(current, following)
    builder.add_edge("admit", "send")
    builder.add_conditional_edges(
        "send",
        admitted_sends,
        ["execute_task", "fan_in"],
    )
    builder.add_edge("execute_task", "fan_in")
    builder.add_edge("fan_in", "verify")
    builder.add_edge("verify", "repair")
    builder.add_edge("repair", "review")
    builder.add_edge("review", "approval")
    builder.add_edge("approval", "complete")
    builder.add_edge("complete", END)
    return builder.compile(checkpointer=checkpointer, name="production-agentic-run")


def _run_stage(
    stage: str,
    executor: RunStageExecutor,
) -> Callable[[RunWorkflowState], Awaitable[dict[str, Any]]]:
    async def execute_stage(state: RunWorkflowState) -> dict[str, Any]:
        request = RunStageRequest.from_state(state, stage)
        if await executor.cancellation_requested(request.run_id):
            raise RunWorkflowCancelled(f"run {request.run_id} cancelled before {stage}")
        result = await executor.execute(request)
        if await executor.cancellation_requested(request.run_id):
            raise RunWorkflowCancelled(f"run {request.run_id} cancelled after {stage}")
        update: dict[str, Any] = {
            "completed_stages": [stage],
            "message_ids": [str(value) for value in result.message_ids],
            "artifact_ids": [str(value) for value in result.artifact_ids],
            "stage_summaries": {stage: result.summary},
        }
        if stage == "admit":
            batch = SchedulerDispatchBatch(
                run_id=request.run_id,
                project_id=request.project_id,
                repository_id=request.repository_id,
                baseline_commit=request.baseline_commit,
                admitted_tasks=result.admitted_tasks,
                completed_task_ids=request.completed_task_ids,
                scheduler_parallel_limit=state["scheduler_parallel_limit"],
            )
            update["admitted_tasks"] = [
                task.model_dump(mode="json") for task in batch.admitted_tasks
            ]
        if result.approval_request_id is not None:
            update["approval_request_id"] = str(result.approval_request_id)
        if stage == "complete":
            update["completion_state"] = "COMPLETED"
        return update

    return execute_stage
