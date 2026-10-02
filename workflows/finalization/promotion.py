"""Evidence-derived memory candidate construction and promotion review."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from domain.enums import ArtifactState, TaskStatus
from domain.models import MemoryCandidate, TaskPlan
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
    plan: TaskPlan | None = None,
    retention_days: int = 180,
) -> MemoryCandidate:
    """Build the durable note for a verified run.

    The content must be reusable knowledge, not a receipt. The previous text
    restated that the run succeeded at a commit, which is a tautology: it
    carried no procedure, so recall could only ever return something the agent
    already knew. It now records the task shape that the evidence actually
    discharged, which is what a later run can act on.
    """
    observed = utc_now()
    procedures: list[str] = []
    if plan is not None:
        for task in plan.tasks:
            criteria = "; ".join(task.acceptance_criteria[:6])
            procedures.append(f"{task.task_type.value}: {task.title} -> {criteria}")
    body = (
        f"Verified delivery of goal {run.goal.strip()[:300]!r} at commit {commit}. "
        f"Integration sink was a {sink.task_type.value} task."
    )
    if procedures:
        body += " Task shapes that the recorded evidence discharged: " + " | ".join(
            procedures[:20]
        ) + "."
    body += " The recorded verification artifacts are authoritative for this outcome."
    return MemoryCandidate(
        candidate_id=uuid5(NAMESPACE_URL, f"memory-candidate:{run.id}:{commit}"),
        project_id=run.project_id,
        source_run_id=run.id,
        source_task_id=sink.id,
        source_attempt_id=attempt.id,
        source_agent="final-reviewer",
        classification="procedural",
        content=body,
        observed_at=observed,
        verified_at=observed,
        # Staleness is enforced here rather than by comparing commits, which made
        # every promoted memory unreachable (writes record the produced commit,
        # queries carry the starting commit).
        valid_until=observed + timedelta(days=retention_days),
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
