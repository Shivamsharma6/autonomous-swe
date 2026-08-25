"""Evidence-derived memory candidate construction and promotion review."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from domain.enums import ArtifactState, TaskStatus
from domain.models import MemoryCandidate
from knowledge.memory.promotion import PromotionReview
from persistence.database import Database
from persistence.tables import (
    AgentMessageRow,
    ArtifactRow,
    RunRow,
    TaskAttemptRow,
    TaskRow,
    ToolExecutionRow,
    utc_now,
)
from tools.gateway import ToolExecutionStatus


def build_memory_candidate(
    *,
    run: RunRow,
    sink: TaskRow,
    attempt: TaskAttemptRow,
    commit: str,
    artifacts: tuple[ArtifactRow, ...],
    messages: tuple[AgentMessageRow, ...],
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=uuid5(NAMESPACE_URL, f"memory-candidate:{run.id}:{commit}"),
        project_id=run.project_id,
        source_run_id=run.id,
        source_task_id=sink.id,
        source_attempt_id=attempt.id,
        source_agent="final-reviewer",
        classification="procedural",
        content=(
            f"Run completed successfully at commit {commit}. The recorded verification "
            "artifacts are authoritative for this outcome."
        ),
        observed_at=utc_now(),
        verified_at=utc_now(),
        repository_id=run.repository_id,
        baseline_commit=commit,
        originating_message_ids=tuple(message.id for message in messages)[:1_000],
        artifact_hashes=tuple(artifact.sha256 for artifact in artifacts)[:1_000],
        verification_commands=(("git", "rev-parse", commit),),
        confidence=0.95,
    )


async def promotion_review(
    database: Database,
    *,
    run_id: UUID,
    sink_id: UUID,
    attempt_status: str,
) -> PromotionReview:
    """Derive promotion-review signals from durable evidence instead of
    rubber-stamped constants so the promotion gate can actually reject."""
    async with database.sessions() as session:
        total_artifacts = int(
            await session.scalar(
                select(func.count())
                .select_from(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
            )
            or 0
        )
        valid_artifacts = int(
            await session.scalar(
                select(func.count())
                .select_from(ArtifactRow)
                .where(
                    ArtifactRow.run_id == run_id,
                    ArtifactRow.state == ArtifactState.VALID,
                )
            )
            or 0
        )
        tool_statuses = tuple(
            (
                await session.scalars(
                    select(ToolExecutionRow.status).where(
                        ToolExecutionRow.task_id == sink_id,
                    )
                )
            ).all()
        )
    outcome_verified = attempt_status == TaskStatus.COMPLETED.value
    artifact_evidence_verified = total_artifacts > 0 and valid_artifacts == total_artifacts
    verification_passed = bool(tool_statuses) and all(
        status == ToolExecutionStatus.COMPLETED.value for status in tool_statuses
    )
    evidence_quality = valid_artifacts / total_artifacts if total_artifacts else 0.0
    structural_checks = (
        outcome_verified,
        bool(tool_statuses),
        total_artifacts > 0,
    )
    structural_quality = sum(1.0 for check in structural_checks if check) / len(
        structural_checks
    )
    return PromotionReview(
        outcome_verified=outcome_verified,
        artifact_evidence_verified=artifact_evidence_verified,
        verification_passed=verification_passed,
        structural_quality=structural_quality,
        evidence_quality=evidence_quality,
        source_kind="distilled",
    )
