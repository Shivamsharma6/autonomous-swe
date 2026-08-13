from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from domain.enums import ArtifactState, TaskType
from domain.models import (
    BudgetPolicy,
    PlanLimits,
    TaskPlan,
    TaskPlanMutation,
    TaskSpec,
)
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.repositories import DomainRepository
from persistence.tables import (
    ArtifactRow,
    AuditEventRow,
    PlanRevisionRow,
    RepairMutationRow,
    TaskRow,
)
from planning.validator import TaskPlanValidator
from workflows.repair import (
    DurableRepairController,
    RepairAction,
    RepairIntegrityError,
    VerificationOutcome,
)
from workflows.review import ReleaseReviewer, ReleaseReviewRequest


async def seed_plan(database: Any) -> tuple[TaskPlan, TaskSpec]:
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    original = TaskSpec(
        id=uuid4(),
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Implement endpoint",
        description="Implement and verify endpoint.",
        task_type=TaskType.IMPLEMENTATION,
        assigned_capability="coder",
        acceptance_criteria=("Endpoint tests pass", "Security validation passes"),
        allowed_tools=("read_file", "apply_patch", "run_tests"),
        budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
    )
    plan = TaskPlan(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="a" * 40,
        revision=1,
        tasks=(original,),
        limits=PlanLimits(
            max_dynamic_tasks=3,
            max_plan_depth=4,
            max_total_budget_usd=10,
            max_total_execution_seconds=1_000,
        ),
    )
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Repair project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/repair.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Build endpoint",
            baseline_commit=plan.baseline_commit,
        )
        await repository.create_plan_revision(
            session,
            run_id=run_id,
            revision=1,
            plan=plan.model_dump(mode="json"),
        )
        await repository.create_task(session, run_id=run_id, task=original)
    return plan, original


@pytest.mark.asyncio
async def test_accepted_repair_resumes_exact_revision_without_duplicate_mutation(
    database: Any,
    tmp_path: Path,
) -> None:
    plan, original = await seed_plan(database)
    repair = TaskSpec(
        id=uuid4(),
        plan_revision=2,
        project_id=plan.project_id,
        repository_id=plan.repository_id,
        title="Repair endpoint",
        description="Repair the exact verification failure.",
        task_type=TaskType.IMPLEMENTATION,
        dependencies=(original.id,),
        assigned_capability="debugger",
        acceptance_criteria=("Original failure no longer reproduces",),
        allowed_tools=("read_file", "apply_patch", "run_tests"),
        budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
    )
    mutation = TaskPlanMutation(
        mutation_id=uuid4(),
        base_revision=1,
        reason="Intentional verification failure",
        tasks=(repair,),
    )
    repository = DomainRepository()
    artifacts = ArtifactService(
        store=ArtifactStore(tmp_path / "repair-evidence"),
        repository=repository,
    )
    async with database.transaction() as session:
        failure_artifact = await artifacts.put(
            session,
            content=b"intentional test failure",
            media_type="text/plain",
            project_id=plan.project_id,
            run_id=plan.run_id,
            task_id=original.id,
        )
    failure = VerificationOutcome(
        passed=False,
        failure_signature="1" * 64,
        progress_fingerprint="2" * 64,
        artifact_ids=(failure_artifact.artifact_id,),
    )
    validator = TaskPlanValidator(allowed_tools={"read_file", "apply_patch", "run_tests"})

    first = await DurableRepairController(database, validator=validator, artifacts=artifacts).apply(
        run_id=plan.run_id,
        outcome=failure,
        mutation=mutation,
    )
    resumed = await DurableRepairController(
        database, validator=validator, artifacts=artifacts
    ).apply(
        run_id=plan.run_id,
        outcome=failure,
        mutation=mutation,
    )

    assert first.action is RepairAction.APPLY_MUTATION
    assert resumed.plan == first.plan
    assert resumed.replayed is True
    with pytest.raises(RepairIntegrityError, match="original run and content"):
        await DurableRepairController(database, validator=validator, artifacts=artifacts).apply(
            run_id=plan.run_id,
            outcome=failure.model_copy(update={"progress_fingerprint": "3" * 64}),
            mutation=mutation,
        )
    async with database.transaction() as session:
        revisions = await session.scalar(select(func.count()).select_from(PlanRevisionRow))
        repairs = await session.scalar(select(func.count()).select_from(RepairMutationRow))
        tasks = await session.scalar(select(func.count()).select_from(TaskRow))
        events = await session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(AuditEventRow.event_type == "plan.repair_accepted")
        )
    assert (revisions, repairs, tasks, events) == (2, 1, 2, 1)


@pytest.mark.asyncio
async def test_final_review_rehashes_evidence_and_excludes_corruption(
    database: Any,
    tmp_path: Path,
) -> None:
    plan, original = await seed_plan(database)
    repository = DomainRepository()
    store = ArtifactStore(tmp_path / "review-artifacts")
    artifacts = ArtifactService(store=store, repository=repository)
    async with database.transaction() as session:
        tests = await artifacts.put(
            session,
            content=b"tests: passed",
            media_type="text/plain",
            project_id=plan.project_id,
            run_id=plan.run_id,
            task_id=original.id,
        )
        security = await artifacts.put(
            session,
            content=b"security: passed",
            media_type="text/plain",
            project_id=plan.project_id,
            run_id=plan.run_id,
            task_id=original.id,
        )
    corrupted_path = store.root / security.storage_key
    corrupted_path.chmod(0o600)
    corrupted_path.write_bytes(b"security: failed")
    reviewer = ReleaseReviewer(database=database, artifacts=artifacts)
    request = ReleaseReviewRequest(
        project_id=plan.project_id,
        run_id=plan.run_id,
        acceptance_criteria=original.acceptance_criteria,
        proposed_evidence={
            "Endpoint tests pass": (tests.artifact_id,),
            "Security validation passes": (security.artifact_id,),
        },
        summary="Review endpoint evidence.",
    )

    decision = await reviewer.review(request)

    assert decision.approved is False
    assert decision.acceptance_evidence == {"Endpoint tests pass": (tests.artifact_id,)}
    assert any("Security validation passes" in reason for reason in decision.failure_reasons)
    async with database.transaction() as session:
        corrupt = await session.get(ArtifactRow, security.artifact_id)
    assert corrupt is not None and corrupt.state is ArtifactState.CORRUPT

    async with database.transaction() as session:
        replacement = await artifacts.put(
            session,
            content=b"security: verified again",
            media_type="text/plain",
            project_id=plan.project_id,
            run_id=plan.run_id,
            task_id=original.id,
        )
    accepted = await reviewer.review(
        request.model_copy(
            update={
                "proposed_evidence": {
                    "Endpoint tests pass": (tests.artifact_id,),
                    "Security validation passes": (replacement.artifact_id,),
                }
            }
        )
    )
    assert accepted.approved is True
    assert set(accepted.acceptance_evidence) == set(original.acceptance_criteria)
    assert accepted.failure_reasons == ()
