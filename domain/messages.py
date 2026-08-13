from __future__ import annotations

import hmac
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from domain.enums import RiskLevel
from domain.models import ContractModel, Sha256, canonical_sha256


class MessageKind(StrEnum):
    CONTEXT_HANDOFF = "context_handoff"
    RESEARCH_EVIDENCE = "research_evidence"
    PATCH_PROPOSAL = "patch_proposal"
    TEST_EVIDENCE = "test_evidence"
    REVIEW_FINDING = "review_finding"
    VALIDATION_RESULT = "validation_result"
    BLOCKER = "blocker"
    TASK_COMPLETION = "task_completion"


class MessageEnvelope(ContractModel):
    message_id: UUID = Field(default_factory=uuid4)
    schema_version: Literal["1.0"] = "1.0"
    kind: MessageKind
    sender: Annotated[str, Field(min_length=1, max_length=100)]
    recipient: Annotated[str, Field(min_length=1, max_length=100)]
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    created_at: AwareDatetime
    causation_id: UUID
    correlation_id: UUID
    artifact_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_content_hash(self) -> MessageEnvelope:
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash and not hmac.compare_digest(self.content_hash, expected):
            raise ValueError("content_hash does not match the message envelope and payload")
        object.__setattr__(self, "content_hash", expected)
        return self


class ContextHandoff(MessageEnvelope):
    kind: Literal[MessageKind.CONTEXT_HANDOFF] = MessageKind.CONTEXT_HANDOFF
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    context_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=1_000)


class ResearchEvidence(MessageEnvelope):
    kind: Literal[MessageKind.RESEARCH_EVIDENCE] = MessageKind.RESEARCH_EVIDENCE
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    findings: tuple[Annotated[str, Field(min_length=1, max_length=20_000)], ...] = Field(
        min_length=1, max_length=1_000
    )
    sources: tuple[Annotated[str, Field(min_length=1, max_length=2_000)], ...] = Field(
        min_length=1, max_length=1_000
    )


class PatchProposal(MessageEnvelope):
    kind: Literal[MessageKind.PATCH_PROPOSAL] = MessageKind.PATCH_PROPOSAL
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    patch_artifact_id: UUID
    changed_paths: tuple[Annotated[str, Field(min_length=1, max_length=1_024)], ...] = Field(
        min_length=1, max_length=1_000
    )


class TestEvidence(MessageEnvelope):
    kind: Literal[MessageKind.TEST_EVIDENCE] = MessageKind.TEST_EVIDENCE
    passed: bool
    commands: tuple[tuple[str, ...], ...] = Field(min_length=1, max_length=100)
    failures: tuple[str, ...] = Field(default_factory=tuple, max_length=1_000)


class ReviewFinding(MessageEnvelope):
    kind: Literal[MessageKind.REVIEW_FINDING] = MessageKind.REVIEW_FINDING
    severity: RiskLevel
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    location: Annotated[str, Field(min_length=1, max_length=2_000)] | None = None


class ValidationResult(MessageEnvelope):
    kind: Literal[MessageKind.VALIDATION_RESULT] = MessageKind.VALIDATION_RESULT
    valid: bool
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    checks: tuple[Annotated[str, Field(min_length=1, max_length=1_000)], ...] = Field(
        min_length=1, max_length=1_000
    )


class Blocker(MessageEnvelope):
    kind: Literal[MessageKind.BLOCKER] = MessageKind.BLOCKER
    code: Annotated[str, Field(min_length=1, max_length=100)]
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]
    retryable: bool


class TaskCompletion(MessageEnvelope):
    kind: Literal[MessageKind.TASK_COMPLETION] = MessageKind.TASK_COMPLETION
    outcome: Literal["COMPLETED", "FAILED", "CANCELLED"]
    summary: Annotated[str, Field(min_length=1, max_length=20_000)]


AgentMessage = Annotated[
    ContextHandoff
    | ResearchEvidence
    | PatchProposal
    | TestEvidence
    | ReviewFinding
    | ValidationResult
    | Blocker
    | TaskCompletion,
    Field(discriminator="kind"),
]


class PersistedMessageRef(ContractModel):
    message_id: UUID
    content_hash: Sha256
