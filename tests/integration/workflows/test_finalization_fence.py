"""Run finalization must be single-shot under concurrent dispatchers.

Regression test for an unfenced stage. `advance_next` selected a run and
released its session before doing any work, with no ownership marker. Two
dispatchers could both select the same run, both observe an approved commit
call, both run `git add --all` against the same worktree, and both promote to the
external memory system. The reviewer LLM, the repair debugger, the commit, and
the memory write are all non-replay-safe. The planner already fences its stage
with a `started_at` timestamp; finalization did not.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from persistence.tables import RunRow, RunStageAttemptRow
from workflows.finalization.core import RunFinalizationService


@pytest.fixture
def service(database, tmp_path) -> RunFinalizationService:
    """A finalizer with no model and no tool registry.

    These tests exercise the ownership fence, which sits in front of the
    side-effecting paths, so the collaborators are deliberately inert: any
    unintended commit or promotion attempt would fail loudly here.
    """

    from domain.models import PlanLimits
    from execution.sandbox.worktrees import GitWorktreeManager
    from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
    from knowledge.memory.fake import FakeMemoryPort
    from persistence.artifacts import ArtifactService, ArtifactStore
    from persistence.repositories import DomainRepository
    from tools.approval import ApprovalService
    from tools.gateway import ToolGateway
    from tools.registry import ToolRegistry

    repository = DomainRepository()
    return RunFinalizationService(
        database=database,
        gateway=ToolGateway(
            database=database,
            registry=ToolRegistry(),
            approvals=ApprovalService(database=database),
        ),
        memory=FakeMemoryPort(),
        artifacts=ArtifactService(
            store=ArtifactStore(tmp_path / "artifacts"),
            repository=repository,
            database=database,
        ),
        scheduler=SchedulerService(
            database=database,
            policy=ConcurrencyPolicy(
                max_parallel_tasks=2,
                max_parallel_tasks_per_project=2,
                max_model_concurrency=2,
                max_sandbox_concurrency=2,
            ),
            lease_ttl=timedelta(minutes=1),
        ),
        worktrees=GitWorktreeManager(tmp_path / "worktrees"),
        primary_model="test-model",
        fallback_models=(),
        limits=PlanLimits(
            max_dynamic_tasks=4,
            max_plan_depth=4,
            max_total_budget_usd=25.0,
            max_total_execution_seconds=7_200,
        ),
    )


async def seed_run(database) -> dict:
    from domain.enums import RunStatus
    from persistence.repositories import DomainRepository

    repository = DomainRepository()
    ids = {name: uuid4() for name in ("project_id", "repository_id", "run_id")}
    async with database.transaction() as session:
        await repository.create_project(session, project_id=ids["project_id"], name="Fence")
        await repository.create_repository(
            session,
            repository_id=ids["repository_id"],
            project_id=ids["project_id"],
            source_path="/imports/fence.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=ids["run_id"],
            project_id=ids["project_id"],
            repository_id=ids["repository_id"],
            goal="Finalize once",
            baseline_commit="a" * 40,
        )
        run = await session.get(RunRow, ids["run_id"])
        assert run is not None
        run.state = RunStatus.EXECUTING.value
        await repository.create_plan_revision(session, run_id=ids["run_id"], revision=1, plan={})
    return ids


async def test_only_one_dispatcher_claims_the_advance_stage(
    database, service
) -> None:
    ids = await seed_run(database)
    state = "EXECUTING"

    first, second, third = await asyncio.gather(
        service._claim_stage(ids["run_id"], state),
        service._claim_stage(ids["run_id"], state),
        service._claim_stage(ids["run_id"], state),
    )

    claims = [claim for claim in (first, second, third) if claim is not None]
    assert len(claims) == 1, "exactly one dispatcher may own the stage"


async def test_a_claim_blocks_a_later_claim(database, service) -> None:
    ids = await seed_run(database)

    first = await service._claim_stage(ids["run_id"], "EXECUTING")
    second = await service._claim_stage(ids["run_id"], "EXECUTING")

    assert first is not None
    assert second is None
    assert await service._holds_stage(ids["run_id"], "EXECUTING", first) is True


async def test_releasing_the_stage_lets_the_next_dispatcher_in(database, service) -> None:
    ids = await seed_run(database)

    first = await service._claim_stage(ids["run_id"], "EXECUTING")
    assert first is not None
    await service._release_stage(ids["run_id"], "EXECUTING", first)

    second = await service._claim_stage(ids["run_id"], "EXECUTING")
    assert second is not None


async def test_a_superseded_owner_loses_its_fence(database, service) -> None:
    """A crashed owner must not be able to act after being taken over."""
    ids = await seed_run(database)

    original = await service._claim_stage(ids["run_id"], "EXECUTING")
    assert original is not None

    # Simulate a takeover by rewriting the fence clock, as a stale reclaim does.
    async with database.transaction() as session:
        attempt = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == ids["run_id"])
        )
        assert attempt is not None
        attempt.started_at = attempt.started_at.replace(year=attempt.started_at.year + 1)

    assert await service._holds_stage(ids["run_id"], "EXECUTING", original) is False


async def test_the_fence_is_recorded_as_durable_state(database, service) -> None:
    ids = await seed_run(database)

    fence = await service._claim_stage(ids["run_id"], "EXECUTING")

    assert fence is not None
    async with database.sessions() as session:
        row = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == ids["run_id"])
        )
    assert row is not None
    assert row.stage == "advance:EXECUTING"
    assert row.status == "RUNNING"


async def test_advance_next_does_not_repeat_a_completed_stage(database, service) -> None:
    ids = await seed_run(database)
    fence = await service._claim_stage(ids["run_id"], "EXECUTING")
    assert fence is not None

    # While the stage is held, advance_next must decline to take it again rather
    # than re-entering the side-effecting path.
    assert await service.advance_next() is None

    async with database.sessions() as session:
        run = await session.get(RunRow, ids["run_id"])
    assert run is not None
    assert run.state == "EXECUTING", "a fenced run must not be advanced twice"
