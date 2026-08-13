from __future__ import annotations

from typing import Annotated
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
