"""Scheduler liveness and ceiling atomicity.

Two regressions covered here.

1. `promote_dependency_ready` scanned a single bounded window of the globally
   oldest PENDING tasks. A task beyond that window whose dependencies were
   already COMPLETED was never examined by any cycle, `claim_ready` only reads
   READY, and the run wedged in EXECUTING forever. The window has to be a batch
   size, not the whole search space.

2. The global capacity ceiling was a read-then-write over `SUM(units)` executed
   under a *per-project* advisory lock. Two projects evaluating the same
   ceiling concurrently both saw room and both inserted, so the documented cap
   was exceeded by up to the number of concurrent dispatchers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, ResourceEstimate, TaskSpec
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from persistence.repositories import DomainRepository
from persistence.tables import ReservationRow, TaskRow


async def seed_chain(database: object, *, blocked: int, runnable: int) -> dict:
    """A project with `blocked` tasks held behind a failure and `runnable` ones
    that are eligible from the start but created later."""
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Promotion project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/promotion.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Promote beyond the window",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})

        # Tasks are created PENDING, which is the state that occupies the front
        # of every ordered promotion window.
        async def add(task_id: UUID, *, order: int, deps: tuple[UUID, ...]) -> TaskSpec:
            task = TaskSpec(
                id=task_id,
                plan_revision=1,
                project_id=project_id,
                repository_id=repository_id,
                title=f"task-{order}",
                description="bounded work",
                task_type=TaskType.IMPLEMENTATION,
                priority=100 - order,
                assigned_capability="coder",
                acceptance_criteria=("done",),
                allowed_tools=("run_tests",),
                risk_ceiling=RiskLevel.MEDIUM,
                budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
                estimate=ResourceEstimate(model_tokens=1_000, sandbox_slots=1),
                dependencies=tuple(str(value) for value in deps),
            )
            await repository.create_task(session, run_id=run_id, task=task)
            return task

        # The oldest tasks stay PENDING forever: the root waits on a dependency
        # that does not exist, which never resolves to COMPLETED, and everything
        # behind it inherits that. They therefore occupy the front of every
        # ordered window forever.
        poisoned = uuid4()
        await add(poisoned, order=0, deps=(uuid4(),))
        for index in range(blocked):
            await add(uuid4(), order=1 + index, deps=(poisoned,))
        # Created last, so they sit behind the whole blocked prefix.
        runnable_ids = tuple(uuid4() for _ in range(runnable))
        for index, task_id in enumerate(runnable_ids):
            await add(task_id, order=1_000 + index, deps=())
    return {
        "project_id": project_id,
        "run_id": run_id,
        "poisoned": poisoned,
        "runnable": runnable_ids,
    }


def service(database: object, *, cap: int = 4, per_project: int = 4) -> SchedulerService:
    return SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=cap,
            max_parallel_tasks_per_project=per_project,
            max_model_concurrency=cap,
            max_sandbox_concurrency=cap,
        ),
        lease_ttl=timedelta(seconds=30),
    )


async def test_promotion_reaches_past_a_full_window_of_blocked_tasks(
    database: object,
) -> None:
    """The wedge: 600 blocked tasks older than 4 runnable ones."""
    seeded = await seed_chain(database, blocked=600, runnable=4)
    scheduler = service(database)

    promoted = await scheduler.promote_dependency_ready(limit=500)

    assert promoted == 4, "every eligible task must be promoted, not just the window"
    assert promoted > 0, "the blocked prefix must not hide eligible work"
    async with database.sessions() as session:
        states = {
            task.id: task.state
            for task in (
                await session.scalars(
                    select(TaskRow).where(TaskRow.id.in_(seeded["runnable"]))
                )
            ).all()
        }
    assert set(states.values()) == {TaskStatus.READY}


async def test_promotion_makes_no_change_when_nothing_is_eligible(database: object) -> None:
    scheduler = service(database)
    await seed_chain(database, blocked=5, runnable=0)

    first = await scheduler.promote_dependency_ready(limit=500)
    second = await scheduler.promote_dependency_ready(limit=500)

    assert first == 0
    assert second == 0


async def test_global_capacity_ceiling_is_not_exceeded_by_concurrent_projects(
    database: object,
) -> None:
    """Two projects must not both observe the last unit of the ceiling."""
    repository = DomainRepository()
    project_ids = []
    for _ in range(2):
        project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
        project_ids.append(project_id)
        async with database.transaction() as session:
            await repository.create_project(session, project_id=project_id, name="Cap project")
            await repository.create_repository(
                session,
                repository_id=repository_id,
                project_id=project_id,
                source_path="/imports/cap.git",
                default_branch="main",
            )
            await repository.create_run(
                session,
                run_id=run_id,
                project_id=project_id,
                repository_id=repository_id,
                goal="Bound capacity",
                baseline_commit="a" * 40,
            )
            await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
            for index in range(4):
                task = TaskSpec(
                    id=uuid4(),
                    plan_revision=1,
                    project_id=project_id,
                    repository_id=repository_id,
                    title=f"cap-{index}",
                    description="bounded work",
                    task_type=TaskType.IMPLEMENTATION,
                    priority=100 - index,
                    assigned_capability="coder",
                    acceptance_criteria=("done",),
                    allowed_tools=("run_tests",),
                    risk_ceiling=RiskLevel.MEDIUM,
                    budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
                    estimate=ResourceEstimate(model_tokens=1_000, sandbox_slots=1),
                )
                await repository.create_task(session, run_id=run_id, task=task)
                await repository.transition_task(
                    session,
                    project_id=project_id,
                    task_id=task.id,
                    expected_version=1,
                    target=TaskStatus.READY,
                )

    scheduler = service(database, cap=4, per_project=4)

    await asyncio.gather(
        scheduler.claim_ready(owner="dispatcher-a", limit=4),
        scheduler.claim_ready(owner="dispatcher-b", limit=4),
    )

    async with database.sessions() as session:
        active = await session.scalar(
            select(func.coalesce(func.sum(ReservationRow.units), 0)).where(
                ReservationRow.released_at.is_(None),
                ReservationRow.resource == "task",
            )
        )
    assert active is not None and active <= 4, (
        f"the global ceiling was exceeded: {active} active task reservations against a cap of 4"
    )


async def test_reclaim_is_bounded_and_releases_capacity(database: object) -> None:
    await seed_chain(database, blocked=0, runnable=2)
    scheduler = service(database)
    await scheduler.promote_dependency_ready(limit=500)
    claims = await scheduler.claim_ready(owner="dispatcher-a", limit=2)
    assert claims

    reclaimed = await scheduler.reclaim_expired(
        now=datetime.now(UTC) + timedelta(seconds=60), limit=10
    )

    assert reclaimed == len(claims)
