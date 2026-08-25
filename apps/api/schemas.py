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


class ProjectFilePayload(ContractModel):
    path: str = Field(min_length=1, max_length=1_000)
    content: str


class ProjectOnboardRequest(ContractModel):
    project_id: UUID = Field(default_factory=uuid4)
    repository_id: UUID = Field(default_factory=uuid4)
    name: Annotated[str, Field(min_length=1, max_length=300)]
    folder_name: Annotated[str, Field(default="", max_length=300)] = ""
    source_path: Annotated[str, Field(default="", max_length=2_000)] = ""
    default_branch: Annotated[str, Field(min_length=1, max_length=255)] = "main"
    files: list[ProjectFilePayload] = Field(default_factory=list)


class ProjectOnboardResponse(ContractModel):
    project_id: UUID
    repository_id: UUID
    name: str
    source_path: str
    default_branch: str
    baseline_commit: str


class ProjectCreated(ContractModel):
    project_id: UUID
    repository_id: UUID


class ModelConfigRequest(ContractModel):
    base_url: str = Field(min_length=1, max_length=2_000)
    api_key: str = Field(default="", max_length=1_000)
    primary_model: str = Field(min_length=1, max_length=200)
    fallback_models: list[str] = Field(default_factory=list, max_length=10)
    timeout_seconds: float = Field(default=300.0, gt=0, le=3_600)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class ModelConfigResponse(ContractModel):
    base_url: str
    primary_model: str
    fallback_models: list[str]
    timeout_seconds: float
    temperature: float
    has_api_key: bool
    api_key_preview: str
    provider_name: str


class ModelProbeRequest(ContractModel):
    base_url: str = Field(min_length=1, max_length=2_000)
    api_key: str = Field(default="", max_length=1_000)


class ModelProbeResponse(ContractModel):
    reachable: bool
    models: list[str]
    latency_ms: float
    error: str | None = None


class ModelTestRequest(ContractModel):
    base_url: str = Field(min_length=1, max_length=2_000)
    api_key: str = Field(default="", max_length=1_000)
    model: str = Field(min_length=1, max_length=200)


class ModelTestResponse(ContractModel):
    success: bool
    model: str
    latency_ms: float
    structured_output: bool
    response_snippet: str
    error: str | None = None


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
