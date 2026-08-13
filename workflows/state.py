from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, computed_field, model_validator

from domain.enums import GraphExecutionState, TaskType
from domain.models import CommitSha, ContractModel, NonEmptyText


def merge_unique(left: list[str], right: list[str]) -> list[str]:
    """Stable, replay-safe reducer for durable IDs and completed node names."""
    result = list(left)
    known = set(left)
    for value in right:
        if value not in known:
            known.add(value)
            result.append(value)
    return result


def merge_summaries(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    return left | right


def merge_dispatch_results(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_task = {str(item["task_id"]): item for item in left}
    for item in right:
        key = str(item["task_id"])
        existing = by_task.get(key)
        if existing is not None and existing != item:
            raise ValueError(f"conflicting replay result for task {key}")
        by_task[key] = item
    return list(by_task.values())


class TaskWorkflowState(TypedDict, total=False):
    trace_id: str
    run_id: str
    project_id: str
    repository_id: str
    task_id: str
    attempt_id: str
    baseline_commit: str
    task_type: str
    goal: str
    input_refs: dict[str, str]
    completed_nodes: Annotated[list[str], merge_unique]
    message_ids: Annotated[list[str], merge_unique]
    artifact_ids: Annotated[list[str], merge_unique]
    summaries: Annotated[dict[str, str], merge_summaries]
    result_id: str


class TaskExecutionInput(ContractModel):
    schema_version: str = "1.0"
    trace_id: str = Field(min_length=1, max_length=500)
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    task_id: UUID
    attempt_id: UUID
    baseline_commit: CommitSha
    task_type: TaskType
    goal: NonEmptyText
    input_refs: dict[str, str] = Field(default_factory=dict, max_length=1_000)

    def to_state(self) -> TaskWorkflowState:
        return TaskWorkflowState(
            trace_id=self.trace_id,
            run_id=str(self.run_id),
            project_id=str(self.project_id),
            repository_id=str(self.repository_id),
            task_id=str(self.task_id),
            attempt_id=str(self.attempt_id),
            baseline_commit=self.baseline_commit,
            task_type=self.task_type.value,
            goal=self.goal,
            input_refs=self.input_refs,
            completed_nodes=[],
            message_ids=[],
            artifact_ids=[],
            summaries={},
        )


class NodeExecutionRequest(ContractModel):
    schema_version: str = "1.0"
    trace_id: str = Field(min_length=1, max_length=500)
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    task_id: UUID
    attempt_id: UUID
    baseline_commit: CommitSha
    task_type: TaskType
    node_name: str = Field(min_length=1, max_length=100)
    goal: NonEmptyText
    input_refs: dict[str, str] = Field(default_factory=dict, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=500)

    @classmethod
    def from_state(cls, state: TaskWorkflowState, node_name: str) -> NodeExecutionRequest:
        identity = (
            f"workflow-node:{state['run_id']}:{state['task_id']}:{state['attempt_id']}:{node_name}"
        )
        return cls(
            trace_id=state["trace_id"],
            run_id=UUID(state["run_id"]),
            project_id=UUID(state["project_id"]),
            repository_id=UUID(state["repository_id"]),
            task_id=UUID(state["task_id"]),
            attempt_id=UUID(state["attempt_id"]),
            baseline_commit=state["baseline_commit"],
            task_type=TaskType(state["task_type"]),
            node_name=node_name,
            goal=state["goal"],
            input_refs=state.get("input_refs", {}),
            idempotency_key=identity,
        )

    def idempotency_uuid(self, output_kind: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"{self.idempotency_key}:{output_kind}")


class NodeExecutionResult(ContractModel):
    schema_version: str = "1.0"
    message_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    artifact_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    result_id: UUID
    summary: str = Field(min_length=1, max_length=2_000)


class TaskGraphResult(ContractModel):
    schema_version: str = "1.0"
    task_id: UUID
    attempt_id: UUID
    task_type: TaskType
    completed_nodes: tuple[str, ...]
    message_ids: tuple[UUID, ...]
    artifact_ids: tuple[UUID, ...]
    result_id: UUID
    summaries: dict[str, str]


class WaitKind(StrEnum):
    TOOL = "tool"
    APPROVAL = "approval"
    UAMS = "uams"


WAIT_GRAPH_STATES: dict[WaitKind, GraphExecutionState] = {
    WaitKind.TOOL: GraphExecutionState.WAITING_FOR_TOOL,
    WaitKind.APPROVAL: GraphExecutionState.WAITING_FOR_APPROVAL,
    WaitKind.UAMS: GraphExecutionState.WAITING_FOR_MEMORY,
}


class CheckpointIdentity(ContractModel):
    schema_version: str = "1.0"
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha

    @computed_field  # type: ignore[prop-decorator]
    @property
    def thread_id(self) -> str:
        return f"run:{self.run_id}:task:{self.task_id}"


class WaitWorkflowState(TypedDict, total=False):
    run_id: str
    task_id: str
    attempt_id: str
    project_id: str
    repository_id: str
    baseline_commit: str
    wait_kind: str
    request_id: str
    resume_payload: dict[str, Any]


class WaitWorkflowInput(ContractModel):
    schema_version: str = "1.0"
    identity: CheckpointIdentity
    wait_kind: WaitKind
    request_id: UUID

    def to_state(self) -> WaitWorkflowState:
        return WaitWorkflowState(
            run_id=str(self.identity.run_id),
            task_id=str(self.identity.task_id),
            attempt_id=str(self.identity.attempt_id),
            project_id=str(self.identity.project_id),
            repository_id=str(self.identity.repository_id),
            baseline_commit=self.identity.baseline_commit,
            wait_kind=self.wait_kind.value,
            request_id=str(self.request_id),
        )


class AdmittedTask(ContractModel):
    task_id: UUID
    task_type: TaskType
    dependencies: tuple[UUID, ...] = Field(default_factory=tuple, max_length=500)


class TaskDispatchResult(ContractModel):
    task_id: UUID
    result_id: UUID
    artifact_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    message_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)


class DispatchWorkflowState(TypedDict, total=False):
    run_id: str
    project_id: str
    repository_id: str
    baseline_commit: str
    admitted_tasks: list[dict[str, Any]]
    completed_task_ids: list[str]
    scheduler_parallel_limit: int
    task: dict[str, Any]
    results: Annotated[list[dict[str, Any]], merge_dispatch_results]
    ordered_task_ids: list[str]


class SchedulerDispatchBatch(ContractModel):
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    admitted_tasks: tuple[AdmittedTask, ...] = Field(default_factory=tuple, max_length=256)
    completed_task_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=500)
    scheduler_parallel_limit: int = Field(ge=1, le=256)

    @model_validator(mode="after")
    def scheduler_admission_is_self_consistent(self) -> SchedulerDispatchBatch:
        task_ids = [task.task_id for task in self.admitted_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("scheduler admission contains duplicate tasks")
        if len(task_ids) > self.scheduler_parallel_limit:
            raise ValueError("scheduler admission exceeds its reserved parallel capacity")
        completed = set(self.completed_task_ids)
        for task in self.admitted_tasks:
            missing = set(task.dependencies).difference(completed)
            if missing:
                raise ValueError(
                    f"scheduler admitted task {task.task_id} before dependencies completed"
                )
        return self

    def to_state(self) -> DispatchWorkflowState:
        return DispatchWorkflowState(
            run_id=str(self.run_id),
            project_id=str(self.project_id),
            repository_id=str(self.repository_id),
            baseline_commit=self.baseline_commit,
            admitted_tasks=[task.model_dump(mode="json") for task in self.admitted_tasks],
            completed_task_ids=[str(value) for value in self.completed_task_ids],
            scheduler_parallel_limit=self.scheduler_parallel_limit,
            results=[],
            ordered_task_ids=[],
        )


class RunWorkflowState(TypedDict, total=False):
    trace_id: str
    run_id: str
    project_id: str
    repository_id: str
    baseline_commit: str
    goal: str
    scheduler_parallel_limit: int
    completed_task_ids: list[str]
    completed_stages: Annotated[list[str], merge_unique]
    message_ids: Annotated[list[str], merge_unique]
    artifact_ids: Annotated[list[str], merge_unique]
    stage_summaries: Annotated[dict[str, str], merge_summaries]
    admitted_tasks: list[dict[str, Any]]
    task: dict[str, Any]
    task_results: Annotated[list[dict[str, Any]], merge_dispatch_results]
    ordered_task_ids: list[str]
    approval_request_id: str
    approval_decision: dict[str, Any]
    completion_state: str


class RunWorkflowInput(ContractModel):
    schema_version: str = "1.0"
    trace_id: str = Field(min_length=1, max_length=500)
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    goal: NonEmptyText
    scheduler_parallel_limit: int = Field(ge=1, le=256)
    completed_task_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=500)

    def to_state(self) -> RunWorkflowState:
        return RunWorkflowState(
            trace_id=self.trace_id,
            run_id=str(self.run_id),
            project_id=str(self.project_id),
            repository_id=str(self.repository_id),
            baseline_commit=self.baseline_commit,
            goal=self.goal,
            scheduler_parallel_limit=self.scheduler_parallel_limit,
            completed_task_ids=[str(value) for value in self.completed_task_ids],
            completed_stages=[],
            message_ids=[],
            artifact_ids=[],
            stage_summaries={},
            admitted_tasks=[],
            task_results=[],
            ordered_task_ids=[],
        )


class RunStageRequest(ContractModel):
    schema_version: str = "1.0"
    trace_id: str = Field(min_length=1, max_length=500)
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    goal: NonEmptyText
    stage: str = Field(min_length=1, max_length=100)
    completed_task_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=500)
    message_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    artifact_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=500)

    @classmethod
    def from_state(cls, state: RunWorkflowState, stage: str) -> RunStageRequest:
        return cls(
            trace_id=state["trace_id"],
            run_id=UUID(state["run_id"]),
            project_id=UUID(state["project_id"]),
            repository_id=UUID(state["repository_id"]),
            baseline_commit=state["baseline_commit"],
            goal=state["goal"],
            stage=stage,
            completed_task_ids=tuple(UUID(value) for value in state.get("completed_task_ids", ())),
            message_ids=tuple(UUID(value) for value in state.get("message_ids", ())),
            artifact_ids=tuple(UUID(value) for value in state.get("artifact_ids", ())),
            idempotency_key=f"run-stage:{state['run_id']}:{stage}",
        )

    def idempotency_uuid(self, output_kind: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"{self.idempotency_key}:{output_kind}")


class RunStageResult(ContractModel):
    schema_version: str = "1.0"
    summary: str = Field(min_length=1, max_length=2_000)
    message_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    artifact_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    admitted_tasks: tuple[AdmittedTask, ...] = Field(default_factory=tuple, max_length=256)
    approval_request_id: UUID | None = None
