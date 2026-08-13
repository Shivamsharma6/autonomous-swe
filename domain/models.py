from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from domain.enums import ApprovalStatus, ArtifactState, RiskLevel, TaskType

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CommitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=20_000)]


def canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_computed_fields=True)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class BudgetPolicy(ContractModel):
    model_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    wall_time_seconds: int = Field(default=0, ge=0)
    cpu_time_ms: int = Field(default=0, ge=0)
    memory_time_byte_seconds: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    network_bytes: int = Field(default=0, ge=0)
    output_bytes: int = Field(default=0, ge=0)


class ResourceEstimate(ContractModel):
    cpu_time_ms: int = Field(default=0, ge=0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    sandbox_slots: int = Field(default=0, ge=0, le=1)
    network_requests: int = Field(default=0, ge=0)
    wall_time_seconds: int = Field(default=0, ge=0)


class RetryPolicy(ContractModel):
    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_backoff_seconds: float = Field(default=1, ge=0, le=3_600)
    maximum_backoff_seconds: float = Field(default=60, ge=0, le=86_400)

    @model_validator(mode="after")
    def backoff_is_ordered(self) -> RetryPolicy:
        if self.initial_backoff_seconds > self.maximum_backoff_seconds:
            raise ValueError("initial backoff cannot exceed maximum backoff")
        return self


class PlanLimits(ContractModel):
    max_dynamic_tasks: int = Field(ge=0, le=1_000)
    max_plan_depth: int = Field(ge=1, le=100)
    max_total_budget_usd: float = Field(gt=0)
    max_total_execution_seconds: int = Field(ge=1, le=604_800)


class TaskSpec(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    plan_revision: int = Field(ge=1)
    project_id: UUID
    repository_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=300)]
    description: NonEmptyText
    task_type: TaskType
    dependencies: tuple[UUID, ...] = Field(default_factory=tuple, max_length=500)
    priority: int = Field(default=0, ge=-1_000, le=1_000)
    assigned_capability: Annotated[str, Field(min_length=1, max_length=100)]
    acceptance_criteria: tuple[Annotated[str, Field(min_length=1, max_length=1_000)], ...] = Field(
        max_length=100
    )
    allowed_tools: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        default_factory=tuple, max_length=100
    )
    risk_ceiling: RiskLevel = RiskLevel.LOW
    expected_artifacts: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        default_factory=tuple, max_length=100
    )
    repository_paths: tuple[Annotated[str, Field(min_length=1, max_length=1_024)], ...] = Field(
        default_factory=tuple, max_length=1_000
    )
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)

    @model_validator(mode="after")
    def dependencies_are_valid(self) -> TaskSpec:
        if self.id in self.dependencies:
            raise ValueError("task cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("task dependencies must be unique")
        return self


class TaskPlan(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    revision: int = Field(ge=1)
    tasks: tuple[TaskSpec, ...] = Field(min_length=1, max_length=500)
    limits: PlanLimits

    @model_validator(mode="after")
    def task_scope_matches_plan(self) -> TaskPlan:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task IDs must be unique within a plan")
        for task in self.tasks:
            if task.project_id != self.project_id or task.repository_id != self.repository_id:
                raise ValueError("task scope must match plan scope")
            if task.plan_revision > self.revision:
                raise ValueError("task creation revision cannot exceed plan revision")
        return self


class TaskPlanMutation(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    mutation_id: UUID = Field(default_factory=uuid4)
    base_revision: int = Field(ge=1)
    reason: NonEmptyText
    tasks: tuple[TaskSpec, ...] = Field(min_length=1, max_length=500)


class AgentSpec(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    role: Annotated[str, Field(min_length=1, max_length=100)]
    purpose: NonEmptyText
    input_schema: Annotated[str, Field(min_length=1, max_length=200)]
    output_schema: Annotated[str, Field(min_length=1, max_length=200)]
    primary_model: Annotated[str, Field(min_length=1, max_length=200)]
    fallback_models: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    tool_grants: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    maximum_risk: RiskLevel
    memory_policy: NonEmptyText
    token_budget: int = Field(ge=0)
    cost_budget_usd: float = Field(ge=0)
    turn_budget: int = Field(ge=1, le=1_000)
    wall_time_seconds: int = Field(ge=1, le=604_800)
    sandbox_profile: Annotated[str, Field(min_length=1, max_length=100)]
    network_profile: Annotated[str, Field(min_length=1, max_length=100)]
    retry_policy: NonEmptyText
    escalation_policy: NonEmptyText
    termination_policy: NonEmptyText

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spec_hash(self) -> str:
        return canonical_sha256(self)


class ArtifactRef(ContractModel):
    artifact_id: UUID
    sha256: Sha256
    media_type: Annotated[str, Field(min_length=1, max_length=200)]
    state: ArtifactState = ArtifactState.VALID


class ImplementationProposal(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    summary: NonEmptyText
    patch: ArtifactRef
    changed_paths: tuple[Annotated[str, Field(min_length=1, max_length=1_024)], ...] = Field(
        min_length=1, max_length=1_000
    )
    verification_commands: tuple[tuple[str, ...], ...] = Field(
        default_factory=tuple, max_length=100
    )


class ReleaseDecision(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    approved: bool
    summary: NonEmptyText
    acceptance_evidence: dict[str, tuple[UUID, ...]] = Field(default_factory=dict)
    failure_reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class SandboxExecution(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    execution_id: UUID
    task_id: UUID
    cpu_time_ms: int = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    peak_processes: int = Field(ge=0)
    processes_created: int | None = Field(default=None, ge=0)
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    network_requests: int = Field(ge=0)
    network_bytes_sent: int = Field(ge=0)
    network_bytes_received: int = Field(ge=0)
    exit_code: int | None
    exit_reason: Annotated[str, Field(min_length=1, max_length=100)]
    limit_triggered: str | None
    measurement_source: Annotated[str, Field(min_length=1, max_length=100)]
    measurement_complete: bool


class MemoryCandidate(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: UUID
    project_id: UUID
    source_run_id: UUID
    source_task_id: UUID
    source_attempt_id: UUID
    source_agent: Annotated[str, Field(min_length=1, max_length=100)]
    classification: Literal["semantic", "episodic", "procedural"]
    content: NonEmptyText
    observed_at: AwareDatetime
    verified_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    repository_id: UUID
    baseline_commit: CommitSha
    originating_message_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1_000)
    artifact_hashes: tuple[Sha256, ...] = Field(min_length=1, max_length=1_000)
    verification_commands: tuple[tuple[str, ...], ...] = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
    supersedes: tuple[UUID, ...] = Field(default_factory=tuple, max_length=100)

    @model_validator(mode="after")
    def freshness_is_ordered(self) -> MemoryCandidate:
        if self.verified_at < self.observed_at:
            raise ValueError("verified_at cannot precede observed_at")
        if self.valid_until is not None and self.valid_until < self.verified_at:
            raise ValueError("valid_until cannot precede verified_at")
        return self


class ToolCallRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    call_id: UUID
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    requested_by: Annotated[str, Field(min_length=1, max_length=100)]
    tool_name: Annotated[str, Field(min_length=1, max_length=100)]
    arguments: dict[str, Any]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=500)]


class ApprovalRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: UUID
    call: ToolCallRequest
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    expires_at: AwareDatetime
    status: ApprovalStatus = ApprovalStatus.PENDING

    @computed_field  # type: ignore[prop-decorator]
    @property
    def call_hash(self) -> str:
        return canonical_sha256(
            {
                "call": self.call.model_dump(mode="json"),
                "project_id": str(self.project_id),
                "repository_id": str(self.repository_id),
                "baseline_commit": self.baseline_commit,
                "expires_at": self.expires_at.isoformat(),
            }
        )


def utc_timestamp(value: datetime) -> float:
    return value.timestamp()
