from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from domain.enums import (
    GRAPH_EXECUTION_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    GraphExecutionState,
    RunStatus,
    TaskStatus,
)
from domain.models import ContractModel, canonical_sha256


class InvalidStateTransition(ValueError):
    pass


def require_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidStateTransition(f"illegal task transition: {current} -> {target}")


def require_graph_transition(current: GraphExecutionState, target: GraphExecutionState) -> None:
    if target not in GRAPH_EXECUTION_TRANSITIONS[current]:
        raise InvalidStateTransition(f"illegal graph transition: {current} -> {target}")


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidStateTransition(f"illegal run transition: {current} -> {target}")


class DomainEvent(ContractModel):
    event_id: UUID = Field(default_factory=uuid4)
    schema_version: Literal["1.0"] = "1.0"
    event_type: str = Field(min_length=1, max_length=200)
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: UUID
    occurred_at: AwareDatetime
    correlation_id: UUID
    causation_id: UUID
    payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


def duration_seconds(entered_at: datetime, exited_at: datetime) -> float:
    return max(0.0, (exited_at - entered_at).total_seconds())
