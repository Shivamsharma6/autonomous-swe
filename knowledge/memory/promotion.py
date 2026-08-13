from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field
from sqlalchemy import select

from domain.enums import GraphExecutionState
from domain.models import ContractModel, MemoryCandidate, canonical_sha256
from knowledge.memory.port import MemoryPort, MemoryUnavailable, MemoryWrite
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, MemoryCandidateRow

_WHITESPACE = re.compile(r"\s+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SENSITIVE_CATEGORIES = frozenset({"identity", "preference", "security"})


class PromotionReview(ContractModel):
    outcome_verified: bool
    artifact_evidence_verified: bool
    verification_passed: bool
    contradiction_found: bool = False
    duplicate_found: bool = False
    structural_quality: float = Field(ge=0, le=1)
    evidence_quality: float = Field(ge=0, le=1)
    source_kind: Literal["distilled", "raw_prompt", "raw_transcript", "full_log", "speculative"]
    sensitive_categories: tuple[str, ...] = ()
    cross_project: bool = False
    human_approved: bool = False


class PromotionDecision(ContractModel):
    accepted: bool
    reasons: tuple[str, ...]
    quality_score: float = Field(ge=0, le=1)


class PromotionOutcome(StrEnum):
    REJECTED = "REJECTED"
    WAITING_FOR_MEMORY = "WAITING_FOR_MEMORY"
    PROMOTED = "PROMOTED"


@dataclass(frozen=True, slots=True)
class PromotionResult:
    outcome: PromotionOutcome
    memory_id: UUID | None
    revision_id: str | None
    reasons: tuple[str, ...] = ()


class PromotionGate:
    def __init__(self, *, minimum_quality: float = 0.75) -> None:
        if not 0 <= minimum_quality <= 1:
            raise ValueError("minimum_quality must be between zero and one")
        self._minimum_quality = minimum_quality

    def evaluate(self, candidate: MemoryCandidate, review: PromotionReview) -> PromotionDecision:
        reasons: list[str] = []
        if not review.outcome_verified:
            reasons.append("outcome_not_verified")
        if not review.artifact_evidence_verified or not candidate.artifact_hashes:
            reasons.append("artifact_evidence_not_verified")
        if not review.verification_passed or not candidate.verification_commands:
            reasons.append("verification_failed")
        if candidate.classification not in {"semantic", "episodic", "procedural"}:
            reasons.append("unsupported_classification")
        if review.contradiction_found:
            reasons.append("contradiction_detected")
        if review.duplicate_found:
            reasons.append("duplicate_detected")
        if review.source_kind != "distilled":
            reasons.append("non_distilled_source")
        if _contains_sensitive_data(candidate.content):
            reasons.append("sensitive_data_detected")
        quality = min(review.structural_quality, review.evidence_quality, candidate.confidence)
        if quality < self._minimum_quality:
            reasons.append("quality_below_threshold")
        approval_sensitive = review.cross_project or bool(
            _SENSITIVE_CATEGORIES.intersection(
                category.casefold() for category in review.sensitive_categories
            )
        )
        if approval_sensitive and not review.human_approved:
            reasons.append("human_approval_required")
        return PromotionDecision(
            accepted=not reasons,
            reasons=tuple(reasons),
            quality_score=quality,
        )


class PromotionService:
    def __init__(
        self,
        database: Any,
        memory: MemoryPort,
        gate: PromotionGate,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._memory = memory
        self._gate = gate
        self._repository = repository or DomainRepository()

    async def promote(self, candidate: MemoryCandidate, review: PromotionReview) -> PromotionResult:
        decision = self._gate.evaluate(candidate, review)
        memory_id = deterministic_memory_id(candidate)
        if not decision.accepted:
            await self._set_local_state(
                candidate,
                status="REJECTED",
                memory_id=memory_id,
                error=", ".join(decision.reasons),
            )
            return PromotionResult(
                outcome=PromotionOutcome.REJECTED,
                memory_id=memory_id,
                revision_id=None,
                reasons=decision.reasons,
            )

        await self._set_local_state(
            candidate,
            status="PROMOTING",
            memory_id=memory_id,
            error=None,
        )
        try:
            receipt = await self._memory.remember(
                MemoryWrite(
                    memory_id=memory_id,
                    candidate=candidate,
                    tags=("#autoswe", "#verified", f"#{candidate.classification}"),
                )
            )
        except MemoryUnavailable as error:
            await self._set_local_state(
                candidate,
                status="WAITING_FOR_MEMORY",
                memory_id=memory_id,
                error=str(error),
                graph_state=GraphExecutionState.WAITING_FOR_MEMORY,
            )
            return PromotionResult(
                outcome=PromotionOutcome.WAITING_FOR_MEMORY,
                memory_id=memory_id,
                revision_id=None,
                reasons=("uams_unavailable",),
            )

        if not receipt.searchable or not receipt.revision_id:
            await self._set_local_state(
                candidate,
                status="WAITING_FOR_MEMORY",
                memory_id=memory_id,
                revision_id=receipt.revision_id,
                error=f"UAMS revision is not searchable: {receipt.status}",
                graph_state=GraphExecutionState.WAITING_FOR_MEMORY,
            )
            return PromotionResult(
                outcome=PromotionOutcome.WAITING_FOR_MEMORY,
                memory_id=memory_id,
                revision_id=receipt.revision_id,
                reasons=("uams_revision_not_searchable",),
            )

        await self._complete(candidate, memory_id=memory_id, revision_id=receipt.revision_id)
        return PromotionResult(
            outcome=PromotionOutcome.PROMOTED,
            memory_id=memory_id,
            revision_id=receipt.revision_id,
        )

    async def _set_local_state(
        self,
        candidate: MemoryCandidate,
        *,
        status: str,
        memory_id: UUID,
        error: str | None,
        revision_id: str | None = None,
        graph_state: GraphExecutionState | None = None,
    ) -> None:
        async with self._database.transaction() as session:
            row = await session.get(MemoryCandidateRow, candidate.candidate_id)
            if row is None:
                raise LookupError(f"memory candidate {candidate.candidate_id} does not exist")
            row.status = status
            row.deterministic_memory_id = memory_id
            row.uams_memory_id = memory_id
            if revision_id is not None:
                row.uams_revision_id = revision_id
            row.last_error = error[:20_000] if error else None
            if graph_state is not None:
                graph = await session.scalar(
                    select(GraphExecutionRow)
                    .where(GraphExecutionRow.task_id == candidate.source_task_id)
                    .with_for_update()
                )
                if graph is not None:
                    graph.state = graph_state
                    graph.state_entered_at = datetime.now(UTC)
            await session.flush()

    async def _complete(
        self, candidate: MemoryCandidate, *, memory_id: UUID, revision_id: str
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            row = await session.get(MemoryCandidateRow, candidate.candidate_id)
            if row is None:
                raise LookupError(f"memory candidate {candidate.candidate_id} does not exist")
            if row.status == "PROMOTED" and row.uams_revision_id == revision_id:
                return
            row.status = "PROMOTED"
            row.deterministic_memory_id = memory_id
            row.uams_memory_id = memory_id
            row.uams_revision_id = revision_id
            row.uams_searchable_at = now
            row.promoted_at = now
            row.last_error = None
            graph = await session.scalar(
                select(GraphExecutionRow)
                .where(GraphExecutionRow.task_id == candidate.source_task_id)
                .with_for_update()
            )
            if graph is not None and graph.state is GraphExecutionState.WAITING_FOR_MEMORY:
                graph.state = GraphExecutionState.RUNNING
                graph.state_entered_at = now
            event_id = uuid5(NAMESPACE_URL, f"autoswe:memory-promoted:{memory_id}:{revision_id}")
            payload = {
                "candidate_id": str(candidate.candidate_id),
                "memory_id": str(memory_id),
                "revision_id": revision_id,
                "project_id": str(candidate.project_id),
                "run_id": str(candidate.source_run_id),
                "task_id": str(candidate.source_task_id),
            }
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type="memory.promoted",
                aggregate_type="memory_candidate",
                aggregate_id=candidate.candidate_id,
                payload=payload,
                correlation_id=candidate.source_run_id,
                causation_id=candidate.candidate_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="memory-promoted",
                payload=payload,
            )
            await session.flush()


def deterministic_memory_id(candidate: MemoryCandidate) -> UUID:
    normalized_content = _WHITESPACE.sub(" ", candidate.content.strip()).casefold()
    content_hash = canonical_sha256(normalized_content)
    value = ":".join(
        (
            "autoswe-memory",
            candidate.schema_version,
            str(candidate.project_id),
            str(candidate.source_run_id),
            str(candidate.source_task_id),
            candidate.classification,
            content_hash,
        )
    )
    return uuid5(NAMESPACE_URL, value)


def _contains_sensitive_data(content: str) -> bool:
    return bool(_EMAIL.search(content) or _SECRET_ASSIGNMENT.search(content))
