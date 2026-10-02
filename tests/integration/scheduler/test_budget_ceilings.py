"""Ceilings must bind before money is spent, not after.

Regression tests for the platform's most expensive failure mode. Every
re-dispatch is a full-cost replay: the dispatcher derives a new attempt id from
a fresh lease token, that attempt gets its own LangGraph thread, and every node
therefore has new idempotency keys and replays from node 0. With no attempt
counter and no run-level accumulator, a worker that kept dying mid-task was
paid for in full every time, indefinitely, and the run never reached FAILED.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, ResourceEstimate, TaskSpec
from execution.scheduler.service import (
    ConcurrencyPolicy,
    SchedulerService,
    TaskBudgetExhausted,
)
from persistence.repositories import DomainRepository
from persistence.tables import ModelCallRow, TaskAttemptRow, TaskRow


async def seed(database: object, *, attempts: int = 0, spent_usd: float = 0.0) -> dict:
    repository = DomainRepository()
    project_id, repository_id, run_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Budget project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/budget.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Bound the spend",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        await repository.create_task(
            session,
            run_id=run_id,
            task=TaskSpec(
                id=task_id,
                plan_revision=1,
                project_id=project_id,
                repository_id=repository_id,
                title="bounded-task",
                description="One bounded task",
                task_type=TaskType.IMPLEMENTATION,
                priority=10,
                assigned_capability="coder",
                acceptance_criteria=("Task completes",),
                allowed_tools=("run_tests",),
                risk_ceiling=RiskLevel.MEDIUM,
                budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
                estimate=ResourceEstimate(model_tokens=1_000, sandbox_slots=1),
            ),
        )
        await repository.transition_task(
            session,
            project_id=project_id,
            task_id=task_id,
            expected_version=1,
            target=TaskStatus.READY,
        )
        for _ in range(attempts):
            await repository.create_attempt(
                session,
                attempt_id=uuid4(),
                task_id=task_id,
                agent_spec_hash="b" * 64,
            )
        if spent_usd:
            # model_calls requires either (task_id, attempt_id) or a run stage
            # attempt, so the accumulated spend is attributed to a real attempt.
            charged_attempt = uuid4()
            await repository.create_attempt(
                session,
                attempt_id=charged_attempt,
                task_id=task_id,
                agent_spec_hash="b" * 64,
            )
            session.add(
                ModelCallRow(
                    id=uuid4(),
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=charged_attempt,
                    trace_id=f"trace-{spent_usd}",
                    turn=1,
                    model="test-model",
                    agent_spec_hash="b" * 64,
                    input_tokens=1_000,
                    output_tokens=1_000,
                    cached_input_tokens=0,
                    cost_usd=spent_usd,
                    validation_errors=[],
                    tool_call_ids=[],
                )
            )
    return {
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
    }


def service(
    database: object, *, max_task_attempts: int = 3, max_run_cost_usd: float = 25.0
) -> SchedulerService:
    return SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=4,
            max_parallel_tasks_per_project=4,
            max_model_concurrency=4,
            max_sandbox_concurrency=4,
            max_task_attempts=max_task_attempts,
            max_run_cost_usd=max_run_cost_usd,
        ),
        lease_ttl=timedelta(seconds=30),
    )


async def start(service_: SchedulerService, seeded: dict) -> object:
    claims = await service_.claim_ready(owner="dispatcher:one", limit=1)
    assert claims, "expected the seeded task to be claimable"
    claim = claims[0]
    return await service_.start_claim(
        task_id=claim.task_id,
        project_id=claim.project_id,
        owner=claim.owner,
        token=claim.token,
        attempt_id=uuid4(),
        agent_spec_hash="b" * 64,
    )


async def test_a_task_within_its_attempt_budget_starts(database: object) -> None:
    seeded = await seed(database, attempts=2)

    lease = await start(service(database, max_task_attempts=3), seeded)

    assert lease.task_id == seeded["task_id"]


async def test_a_task_past_its_attempt_budget_is_refused(database: object) -> None:
    seeded = await seed(database, attempts=3)

    with pytest.raises(TaskBudgetExhausted, match="attempt budget"):
        await start(service(database, max_task_attempts=3), seeded)


async def test_an_exhausted_task_is_left_failed_not_runnable(database: object) -> None:
    """The wedge this closes: a re-queued task that can never succeed.

    Previously reconciliation re-queued the task forever and it never reached
    FAILED, so the run stayed EXECUTING and every retry was paid for again.
    """
    seeded = await seed(database, attempts=5)

    with pytest.raises(TaskBudgetExhausted):
        await start(service(database, max_task_attempts=3), seeded)

    async with database.sessions() as session:
        task = await session.get(TaskRow, seeded["task_id"])
    assert task is not None
    assert task.state is TaskStatus.FAILED


async def test_an_exhausted_task_releases_its_lease_and_reservations(database: object) -> None:
    from sqlalchemy import select

    from persistence.tables import LeaseRow, ReservationRow

    seeded = await seed(database, attempts=9)

    with pytest.raises(TaskBudgetExhausted):
        await start(service(database, max_task_attempts=3), seeded)

    async with database.sessions() as session:
        leases = tuple((await session.scalars(select(LeaseRow))).all())
        reservations = tuple((await session.scalars(select(ReservationRow))).all())
    assert leases == (), "an exhausted task must not hold a lease"
    assert all(row.released_at is not None for row in reservations), (
        "capacity must be returned so other tasks are not starved by a dead task"
    )


async def test_a_run_past_its_cost_ceiling_is_refused(database: object) -> None:
    seeded = await seed(database, attempts=0, spent_usd=25.5)

    with pytest.raises(TaskBudgetExhausted, match="cost budget"):
        await start(service(database, max_run_cost_usd=25.0), seeded)


async def test_cost_ceiling_does_not_block_a_run_within_budget(database: object) -> None:
    seeded = await seed(database, attempts=0, spent_usd=1.25)

    lease = await start(service(database, max_run_cost_usd=25.0), seeded)

    assert lease.run_id == seeded["run_id"]


async def test_a_started_attempt_is_recorded_beyond_the_seeded_ones(database: object) -> None:
    """The cap counts real attempts, and a legitimate start still records one."""
    from sqlalchemy import func, select

    seeded = await seed(database, attempts=1)

    lease = await start(service(database, max_task_attempts=3), seeded)

    async with database.sessions() as session:
        recorded = await session.scalar(
            select(func.count())
            .select_from(TaskAttemptRow)
            .where(TaskAttemptRow.task_id == seeded["task_id"])
        )
    assert lease.task_id == seeded["task_id"]
    assert recorded == 2
