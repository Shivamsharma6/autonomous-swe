from __future__ import annotations

from enum import StrEnum


class TaskType(StrEnum):
    RESEARCH = "RESEARCH"
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST = "TEST"
    REFACTOR = "REFACTOR"
    DOCUMENTATION = "DOCUMENTATION"
    VALIDATION = "VALIDATION"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class GraphExecutionState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_MEMORY = "WAITING_FOR_MEMORY"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ArtifactState(StrEnum):
    PENDING = "PENDING"
    VALID = "VALID"
    CORRUPT = "CORRUPT"
    QUARANTINED = "QUARANTINED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class RetryCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    POLICY = "POLICY"
    BUDGET = "BUDGET"
    CANCELLATION = "CANCELLATION"
    UNCERTAIN_SIDE_EFFECT = "UNCERTAIN_SIDE_EFFECT"


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset(
        {TaskStatus.LEASED, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.LEASED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.BLOCKED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


GRAPH_EXECUTION_TRANSITIONS: dict[GraphExecutionState, frozenset[GraphExecutionState]] = {
    GraphExecutionState.NOT_STARTED: frozenset(
        {GraphExecutionState.RUNNING, GraphExecutionState.CANCELLED}
    ),
    GraphExecutionState.RUNNING: frozenset(
        {
            GraphExecutionState.WAITING_FOR_TOOL,
            GraphExecutionState.WAITING_FOR_APPROVAL,
            GraphExecutionState.WAITING_FOR_MEMORY,
            GraphExecutionState.PAUSED,
            GraphExecutionState.COMPLETED,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
            GraphExecutionState.NEEDS_RECONCILIATION,
        }
    ),
    GraphExecutionState.WAITING_FOR_TOOL: frozenset(
        {
            GraphExecutionState.RUNNING,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
            GraphExecutionState.NEEDS_RECONCILIATION,
        }
    ),
    GraphExecutionState.WAITING_FOR_APPROVAL: frozenset(
        {
            GraphExecutionState.RUNNING,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
        }
    ),
    GraphExecutionState.WAITING_FOR_MEMORY: frozenset(
        {
            GraphExecutionState.RUNNING,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
        }
    ),
    GraphExecutionState.PAUSED: frozenset(
        {GraphExecutionState.RUNNING, GraphExecutionState.FAILED, GraphExecutionState.CANCELLED}
    ),
    GraphExecutionState.COMPLETED: frozenset(),
    GraphExecutionState.FAILED: frozenset(),
    GraphExecutionState.CANCELLED: frozenset(),
    GraphExecutionState.NEEDS_RECONCILIATION: frozenset(
        {
            GraphExecutionState.RUNNING,
            GraphExecutionState.COMPLETED,
            GraphExecutionState.FAILED,
            GraphExecutionState.CANCELLED,
        }
    ),
}
