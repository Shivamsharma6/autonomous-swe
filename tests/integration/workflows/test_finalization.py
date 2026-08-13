from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from agents.gateway import ModelResponse, ModelUsage
from agents.scripted import ScriptedGateway, ScriptedResponse
from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.messages import ContextHandoff
from domain.models import BudgetPolicy, PlanLimits, TaskPlan, TaskSpec
from execution.sandbox.worktrees import GitWorktreeManager
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from knowledge.memory.fake import FakeMemoryPort
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.repositories import DomainRepository
from persistence.tables import (
    ApprovalRow,
    ArtifactRow,
    RunRow,
    RunStageAttemptRow,
    TaskAttemptRow,
    ToolExecutionRow,
)
from workflows.finalization import RunFinalizationService

GIT = shutil.which("git")


def git(repository: Path, *arguments: str) -> str:
    if GIT is None:
        pytest.skip("Git is unavailable")
    return subprocess.run(  # noqa: S603 - isolated test repository and fixed arguments
        (GIT, "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_verified_run_requires_exact_approval_then_commits_promotes_and_cleans_up(
    database, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    (source / "app.py").write_text("VALUE = 1\n")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "initial")
    baseline = git(source, "rev-parse", "HEAD")

    repository = DomainRepository()
    project_id, repository_id, run_id, task_id, attempt_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    limits = PlanLimits(
        max_dynamic_tasks=4,
        max_plan_depth=4,
        max_total_budget_usd=10,
        max_total_execution_seconds=1_000,
    )
    task_spec = TaskSpec(
        id=task_id,
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Final validation",
        description="Verify the integrated implementation.",
        task_type=TaskType.VALIDATION,
        assigned_capability="validation",
        acceptance_criteria=("Integrated verification passes",),
        allowed_tools=("read_file", "run_tests"),
        risk_ceiling=RiskLevel.MEDIUM,
        budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
    )
    plan = TaskPlan(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit=baseline,
        revision=1,
        tasks=(task_spec,),
        limits=limits,
    )
    worktrees = GitWorktreeManager(tmp_path / "worktrees")
    worktree = worktrees.create_task_worktree(source, task_id, baseline)
    (worktree / "app.py").write_text("VALUE = 2\n")
    artifacts = ArtifactService(
        store=ArtifactStore(tmp_path / "artifacts"),
        repository=repository,
    )
    now = datetime.now(UTC)
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="finalize")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path=str(source),
            default_branch="main",
        )
        run = await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Change the application value",
            baseline_commit=baseline,
        )
        run.state = "EXECUTING"
        run.state_entered_at = now
        await repository.create_plan_revision(
            session,
            run_id=run_id,
            revision=1,
            plan=plan.model_dump(mode="json"),
        )
        task = await repository.create_task(session, run_id=run_id, task=task_spec)
        task.state = TaskStatus.COMPLETED
        task.state_entered_at = now
        await repository.create_attempt(
            session,
            attempt_id=attempt_id,
            task_id=task_id,
            agent_spec_hash="f" * 64,
        )
        attempt = await session.get(TaskAttemptRow, attempt_id)
        assert attempt is not None
        attempt.status = "COMPLETED"
        attempt.ended_at = now
        stored = await artifacts.put(
            session,
            content=json.dumps(
                {
                    "output": {
                        "summary": "Integrated checks passed",
                        "verification_passed": True,
                    }
                }
            ).encode(),
            media_type="application/vnd.autoswe.node-result+json",
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
        )
        await repository.persist_message(
            session,
            ContextHandoff(
                sender="validation",
                recipient="workflow",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                created_at=now,
                causation_id=uuid4(),
                correlation_id=run_id,
                artifact_ids=(stored.artifact_id,),
                summary="Integrated checks passed",
            ),
        )

    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=2,
            max_parallel_tasks_per_project=2,
            max_model_concurrency=2,
            max_sandbox_concurrency=2,
        ),
        lease_ttl=timedelta(minutes=1),
    )
    memory = FakeMemoryPort()
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                response=ModelResponse(
                    trace_id="scripted",
                    provider_request_id="release-review-1",
                    model="scripted-model",
                    structured_output={
                        "approved": True,
                        "summary": "All acceptance criteria have verified evidence.",
                        "acceptance_evidence": {
                            "Integrated verification passes": [str(stored.artifact_id)]
                        },
                        "failure_reasons": [],
                    },
                    finish_reason="stop",
                    usage=ModelUsage(input_tokens=50, output_tokens=25, cost_usd=0.01),
                )
            ),
        )
    )
    finalizer = RunFinalizationService(
        database=database,
        gateway=gateway,
        memory=memory,
        artifacts=artifacts,
        scheduler=scheduler,
        worktrees=worktrees,
        primary_model="scripted-model",
        fallback_models=(),
        limits=limits,
    )

    assert await finalizer.advance_next() == "WAITING_FOR_APPROVAL"
    async with database.sessions() as session:
        approval = await session.scalar(select(ApprovalRow))
        execution = await session.scalar(select(ToolExecutionRow))
        run = await session.get(RunRow, run_id)
        review = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.stage == "final-review:1")
        )
        review_artifact = await session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.media_type
                == "application/vnd.autoswe.release-decision+json"
            )
        )
    assert approval is not None and execution is not None
    assert run is not None and run.state == "WAITING_FOR_APPROVAL"
    assert execution.tool_name == "git_commit"
    assert review is not None and review.status == "COMPLETED"
    assert review_artifact is not None and review_artifact.state.value == "VALID"
    assert len(gateway.requests) == 1

    await finalizer._approvals.decide(  # noqa: SLF001 - exercises exact production binding
        approval.id,
        approver="operator@example.invalid",
        approved=True,
        expected_call_hash=approval.call_hash,
    )
    assert await finalizer.advance_next() == "COMPLETED"

    async with database.sessions() as session:
        run = await session.get(RunRow, run_id)
        execution = await session.scalar(select(ToolExecutionRow))
    assert run is not None and run.state == "COMPLETED"
    assert execution is not None and execution.status == "COMPLETED"
    commit = execution.result["output"]["commit"]
    assert git(source, "rev-parse", f"autoswe/task/{task_id}") == commit
    assert memory.remembered
    assert not worktree.exists()
