from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, ResourceEstimate, TaskSpec
from execution.scheduler.service import ConcurrencyPolicy, ResourceObservation, SchedulerService
from persistence.repositories import DomainRepository
from persistence.tables import ProjectTaskResourceEstimateRow, ReservationRow


async def seed_ready_tasks(database: object, count: int) -> dict[str, object]:
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    tasks: list[TaskSpec] = []
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Scheduler project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/scheduler.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Schedule bounded work",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        for index in range(count):
            task = TaskSpec(
                id=UUID(int=index + 1),
                plan_revision=1,
                project_id=project_id,
                repository_id=repository_id,
                title=f"task-{index}",
                description=f"Execute scheduler task {index}",
                task_type=TaskType.IMPLEMENTATION,
                priority=100 - index,
                assigned_capability="coder",
                acceptance_criteria=("Task executes once",),
                allowed_tools=("run_tests",),
                risk_ceiling=RiskLevel.MEDIUM,
                budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
                estimate=ResourceEstimate(
                    model_tokens=1_000,
                    sandbox_slots=1,
                    wall_time_seconds=60,
                ),
            )
            await repository.create_task(session, run_id=run_id, task=task)
            await repository.transition_task(
                session,
                project_id=project_id,
                task_id=task.id,
                expected_version=1,
                target=TaskStatus.READY,
            )
            tasks.append(task)
    return {
        "project_id": project_id,
        "repository_id": repository_id,
        "run_id": run_id,
        "tasks": tuple(tasks),
    }


def scheduler(database: object, *, per_project: int = 2) -> SchedulerService:
    return SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=2,
            max_parallel_tasks_per_project=per_project,
            max_model_concurrency=2,
            max_sandbox_concurrency=2,
        ),
        lease_ttl=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_skip_locked_claim_gives_one_task_to_one_dispatcher(database: object) -> None:
    seeded = await seed_ready_tasks(database, 1)
    service = scheduler(database)

    first, second = await asyncio.gather(
        service.claim_ready(owner="dispatcher-a", limit=1),
        service.claim_ready(owner="dispatcher-b", limit=1),
    )

    claims = first + second
    assert len(claims) == 1
    assert claims[0].task_id == seeded["tasks"][0].id
    assert claims[0].owner in {"dispatcher-a", "dispatcher-b"}


@pytest.mark.asyncio
async def test_reservations_enforce_project_model_and_sandbox_capacity(database: object) -> None:
    await seed_ready_tasks(database, 3)
    service = scheduler(database, per_project=1)

    claimed = await service.claim_ready(owner="dispatcher-a", limit=10)
    blocked = await service.claim_ready(owner="dispatcher-b", limit=10)

    assert len(claimed) == 1
    assert blocked == ()
    async with database.transaction() as session:
        active = await session.execute(
            select(ReservationRow.resource, func.sum(ReservationRow.units))
            .where(ReservationRow.released_at.is_(None))
            .group_by(ReservationRow.resource)
        )
        totals = dict(active.all())
    assert totals == {"model": 1, "sandbox": 1, "task": 1}


@pytest.mark.asyncio
async def test_only_lease_owner_can_heartbeat(database: object) -> None:
    await seed_ready_tasks(database, 1)
    service = scheduler(database)
    before = datetime.now(UTC)
    claim = (await service.claim_ready(owner="dispatcher-a", limit=1, now=before))[0]

    assert await service.heartbeat(
        task_id=claim.task_id,
        owner="dispatcher-a",
        token=claim.token,
        now=before + timedelta(seconds=10),
    )
    assert not await service.heartbeat(
        task_id=claim.task_id,
        owner="dispatcher-b",
        token=claim.token,
        now=before + timedelta(seconds=11),
    )
    assert not await service.heartbeat(
        task_id=claim.task_id,
        owner="dispatcher-a",
        token=uuid4(),
        now=before + timedelta(seconds=11),
    )


@pytest.mark.asyncio
async def test_expired_lease_releases_reservations_once_and_requeues(database: object) -> None:
    seeded = await seed_ready_tasks(database, 1)
    service = scheduler(database)
    started = datetime.now(UTC)
    await service.claim_ready(owner="dispatcher-a", limit=1, now=started)

    assert await service.reclaim_expired(now=started + timedelta(seconds=31)) == 1
    assert await service.reclaim_expired(now=started + timedelta(seconds=32)) == 0

    async with database.transaction() as session:
        task = await DomainRepository().get_task(
            session,
            project_id=seeded["project_id"],
            task_id=seeded["tasks"][0].id,
        )
        active = await session.scalar(
            select(func.count())
            .select_from(ReservationRow)
            .where(ReservationRow.released_at.is_(None))
        )
    assert task is not None
    assert task.state == TaskStatus.READY
    assert active == 0


@pytest.mark.asyncio
async def test_running_expired_lease_requires_checkpoint_reconciliation(database: object) -> None:
    seeded = await seed_ready_tasks(database, 1)
    service = scheduler(database)
    started = datetime.now(UTC)
    claim = (await service.claim_ready(owner="dispatcher-a", limit=1, now=started))[0]
    repository = DomainRepository()
    async with database.transaction() as session:
        row = await repository.get_task(
            session,
            project_id=seeded["project_id"],
            task_id=claim.task_id,
        )
        assert row is not None
        await repository.transition_task(
            session,
            project_id=seeded["project_id"],
            task_id=claim.task_id,
            expected_version=row.version,
            target=TaskStatus.RUNNING,
        )

    assert await service.reclaim_expired(now=started + timedelta(seconds=31)) == 0

    async with database.transaction() as session:
        row = await repository.get_task(
            session,
            project_id=seeded["project_id"],
            task_id=claim.task_id,
        )
        active = await session.scalar(
            select(func.count())
            .select_from(ReservationRow)
            .where(ReservationRow.released_at.is_(None))
        )
    assert row is not None
    assert row.state is TaskStatus.RUNNING
    assert active == 3


@pytest.mark.asyncio
async def test_cancellation_commits_before_notification(database: object) -> None:
    seeded = await seed_ready_tasks(database, 1)
    service = scheduler(database)
    task_id = seeded["tasks"][0].id
    notifications: list[TaskStatus] = []

    async def notify(_: UUID) -> None:
        async with database.transaction() as session:
            row = await DomainRepository().get_task(
                session,
                project_id=seeded["project_id"],
                task_id=task_id,
            )
            assert row is not None
            notifications.append(row.state)

    await service.cancel_task(
        project_id=seeded["project_id"],
        task_id=task_id,
        notify=notify,
    )

    assert notifications == [TaskStatus.CANCELLED]


@pytest.mark.asyncio
async def test_actual_usage_updates_project_task_type_rolling_estimates(database: object) -> None:
    seeded = await seed_ready_tasks(database, 1)
    service = scheduler(database)

    await service.record_observed_usage(
        project_id=seeded["project_id"],
        task_type=TaskType.IMPLEMENTATION,
        observation=ResourceObservation(
            cpu_time_ms=100,
            peak_memory_bytes=1_000,
            duration_ms=200,
            output_bytes=300,
            network_requests=1,
            model_tokens=400,
            cost_usd=0.4,
        ),
    )
    await service.record_observed_usage(
        project_id=seeded["project_id"],
        task_type=TaskType.IMPLEMENTATION,
        observation=ResourceObservation(
            cpu_time_ms=300,
            peak_memory_bytes=2_000,
            duration_ms=600,
            output_bytes=700,
            network_requests=3,
            model_tokens=800,
            cost_usd=0.8,
        ),
    )

    async with database.transaction() as session:
        estimate = await session.scalar(select(ProjectTaskResourceEstimateRow))

    assert estimate is not None
    assert estimate.sample_count == 2
    assert estimate.average_cpu_time_ms == 200
    assert estimate.peak_memory_bytes == 2_000
    assert estimate.average_duration_ms == 400
    assert estimate.average_output_bytes == 500
    assert estimate.average_network_requests == 2
    assert estimate.average_model_tokens == 600
    assert estimate.average_cost_usd == pytest.approx(0.6)
