from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field

from domain.models import CommitSha, ContractModel, NonEmptyText, Sha256


class ProjectCreateRequest(ContractModel):
    project_id: UUID = Field(default_factory=uuid4)
    repository_id: UUID = Field(default_factory=uuid4)
    name: Annotated[str, Field(min_length=1, max_length=300)]
    source_path: Annotated[str, Field(min_length=1, max_length=2_000)]
    default_branch: Annotated[str, Field(min_length=1, max_length=255)] = "main"


class ProjectCreated(ContractModel):
    project_id: UUID
    repository_id: UUID


class RunCreateRequest(ContractModel):
    run_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    repository_id: UUID
    goal: NonEmptyText
    baseline_commit: CommitSha


class RunCreated(ContractModel):
    run_id: UUID
    state: str


class TaskResponse(ContractModel):
    task_id: UUID
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    state: str
    version: int
    task_type: str
    title: str
    state_entered_at: str
    plan_revision: int
    dependencies: tuple[UUID, ...]
    assigned_capability: str
    acceptance_criteria: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    risk_ceiling: str


class RunResponse(ContractModel):
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    goal: str
    baseline_commit: str
    state: str
    state_entered_at: str
    state_duration_seconds: float
    active_plan_revision: int | None
    task_counts: dict[str, int]
    model_input_tokens: int
    model_output_tokens: int
    model_cost_usd: float
    created_at: str
    updated_at: str


class ApprovalResponse(ContractModel):
    approval_id: UUID
    call_id: UUID
    status: str
    call_hash: Sha256
    tool_name: str
    requested_by: str
    arguments: dict[str, Any]
    expires_at: str
    created_at: str
    decided_at: str | None
    approver: str | None


class ArtifactMetadataResponse(ContractModel):
    artifact_id: UUID
    task_id: UUID
    sha256: Sha256
    media_type: str
    state: str
    size_bytes: int
    verified_at: str | None
    created_at: str


class AuditEventResponse(ContractModel):
    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    content_hash: Sha256
    created_at: str


class ApprovalDecisionRequest(ContractModel):
    approved: bool
    approver: Annotated[str, Field(min_length=1, max_length=255)]
    expected_call_hash: Sha256


class DeadLetterResponse(ContractModel):
    dead_letter_id: UUID
    event_id: UUID
    consumer: str
    topic: str
    attempts: int
    last_error: str
    created_at: str
    resolved: bool
