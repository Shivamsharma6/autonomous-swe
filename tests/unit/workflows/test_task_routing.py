from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from domain.enums import TaskType
from workflows.feature import RUN_STAGE_SEQUENCE, build_run_graph
from workflows.state import (
    AdmittedTask,
    NodeExecutionRequest,
    NodeExecutionResult,
    RunStageRequest,
    RunStageResult,
    RunWorkflowInput,
    TaskDispatchResult,
    TaskExecutionInput,
)
from workflows.task_subgraphs import (
    TASK_NODE_SEQUENCES,
    WorkflowCancelled,
    build_task_subgraph,
    task_result_from_state,
)

EXPECTED_ROUTES = {
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


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[NodeExecutionRequest] = []

    async def cancellation_requested(self, _: UUID) -> bool:
        return False

    async def execute(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        self.requests.append(request)
        return NodeExecutionResult(
            message_ids=(request.idempotency_uuid("message"),),
            artifact_ids=(request.idempotency_uuid("artifact"),),
            result_id=request.idempotency_uuid("result"),
            summary=f"completed {request.node_name}",
        )


class RecordingRunExecutor:
    def __init__(self, tasks: tuple[AdmittedTask, ...]) -> None:
        self.tasks = tasks
        self.requests: list[RunStageRequest] = []

    async def cancellation_requested(self, _: UUID) -> bool:
        return False

    async def execute(self, request: RunStageRequest) -> RunStageResult:
        self.requests.append(request)
        return RunStageResult(
            summary=f"completed {request.stage}",
            admitted_tasks=self.tasks if request.stage == "admit" else (),
            message_ids=(request.idempotency_uuid("message"),),
            artifact_ids=(request.idempotency_uuid("artifact"),),
        )


class RecordingTaskRunner:
    def __init__(self) -> None:
        self.tasks: list[AdmittedTask] = []

    async def execute(self, task: AdmittedTask) -> TaskDispatchResult:
        self.tasks.append(task)
        return TaskDispatchResult(
            task_id=task.task_id,
            result_id=uuid4(),
        )


class CancelAfterExecution(RecordingExecutor):
    async def cancellation_requested(self, _: UUID) -> bool:
        return bool(self.requests)


def task_input(task_type: TaskType) -> TaskExecutionInput:
    return TaskExecutionInput(
        trace_id=f"trace-{uuid4()}",
        run_id=uuid4(),
        project_id=uuid4(),
        repository_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        baseline_commit="a" * 40,
        task_type=task_type,
        goal=f"Execute {task_type.value} task",
    )


@pytest.mark.parametrize("task_type", tuple(TaskType))
@pytest.mark.asyncio
async def test_task_type_selects_only_its_approved_nodes(task_type: TaskType) -> None:
    executor = RecordingExecutor()
    graph = build_task_subgraph(task_type, executor=executor)
    execution = task_input(task_type)

    state = await graph.ainvoke(execution.to_state())
    result = task_result_from_state(state)

    assert TASK_NODE_SEQUENCES[task_type] == EXPECTED_ROUTES[task_type]
    assert tuple(request.node_name for request in executor.requests) == EXPECTED_ROUTES[task_type]
    assert result.completed_nodes == EXPECTED_ROUTES[task_type]
    assert len(result.message_ids) == len(EXPECTED_ROUTES[task_type])
    assert len(result.artifact_ids) == len(EXPECTED_ROUTES[task_type])
    assert result.result_id == executor.requests[-1].idempotency_uuid("result")
    assert all(request.idempotency_key for request in executor.requests)


@pytest.mark.asyncio
async def test_replayed_node_outputs_merge_without_duplicate_durable_ids() -> None:
    executor = RecordingExecutor()
    execution = task_input(TaskType.VALIDATION)
    graph = build_task_subgraph(TaskType.VALIDATION, executor=executor)

    first = await graph.ainvoke(execution.to_state())
    second = await graph.ainvoke(first)
    result = task_result_from_state(second)

    assert len(result.message_ids) == len(EXPECTED_ROUTES[TaskType.VALIDATION])
    assert len(result.artifact_ids) == len(EXPECTED_ROUTES[TaskType.VALIDATION])


@pytest.mark.asyncio
async def test_top_level_graph_runs_full_pipeline_and_only_admitted_task_sends() -> None:
    admitted = (
        AdmittedTask(task_id=uuid4(), task_type=TaskType.RESEARCH),
        AdmittedTask(task_id=uuid4(), task_type=TaskType.IMPLEMENTATION),
    )
    stage_executor = RecordingRunExecutor(admitted)
    task_runner = RecordingTaskRunner()
    graph = build_run_graph(stage_executor=stage_executor, task_runner=task_runner)
    run_input = RunWorkflowInput(
        trace_id=f"trace-{uuid4()}",
        run_id=uuid4(),
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="d" * 40,
        goal="Build a production SaaS service.",
        scheduler_parallel_limit=2,
    )

    state = await graph.ainvoke(run_input.to_state())

    assert tuple(request.stage for request in stage_executor.requests) == RUN_STAGE_SEQUENCE
    assert {task.task_id for task in task_runner.tasks} == {task.task_id for task in admitted}
    assert state["ordered_task_ids"] == sorted(str(task.task_id) for task in admitted)
    assert state["completion_state"] == "COMPLETED"
    assert all(request.idempotency_key for request in stage_executor.requests)


@pytest.mark.asyncio
async def test_task_node_checks_cancellation_after_external_io() -> None:
    executor = CancelAfterExecution()
    execution = task_input(TaskType.IMPLEMENTATION)
    graph = build_task_subgraph(TaskType.IMPLEMENTATION, executor=executor)

    with pytest.raises(WorkflowCancelled, match="cancelled after recall"):
        await graph.ainvoke(execution.to_state())

    assert [request.node_name for request in executor.requests] == ["recall"]
