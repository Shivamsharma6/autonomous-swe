from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from apps.dispatcher.main import DispatchMessage
from apps.worker.executor import DispatchedTaskExecutor, TaskExecutionContext
from apps.worker.runner import RedisDispatchInbox, WorkerOutcome, WorkerService
from domain.enums import GraphExecutionState, RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, TaskSpec
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from messaging.redis_streams import RedisStreamsTransport
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, LeaseRow, ReservationRow, TaskAttemptRow, TaskRow
from workflows.state import NodeExecutionRequest, NodeExecutionResult


class RecordingNodeExecutor:
    def __init__(self) -> None:
        self.nodes: list[str] = []

    async def cancellation_requested(self, task_id):  # type: ignore[no-untyped-def]
        return False

    async def execute(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        self.nodes.append(request.node_name)
        return NodeExecutionResult(
            result_id=request.idempotency_uuid("result"),
            summary=f"completed {request.node_name}",
        )


async def _seed_claim(database, *, max_parallel=2):  # type: ignore[no-untyped-def]
    repository = DomainRepository()
    project_id = uuid4()
    repository_id = uuid4()
    run_id = uuid4()
    task_id = uuid4()
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="worker project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/project.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Inspect the repository",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        task = await repository.create_task(
            session,
            run_id=run_id,
            task=TaskSpec(
                id=task_id,
                plan_revision=1,
                project_id=project_id,
                repository_id=repository_id,
                title="Inspect",
                description="Inspect and verify the repository.",
                task_type=TaskType.VALIDATION,
                assigned_capability="validation",
                acceptance_criteria=("Evidence exists",),
                allowed_tools=("read_file",),
                risk_ceiling=RiskLevel.LOW,
                budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
            ),
        )
        await repository.transition_task(
            session,
            project_id=project_id,
            task_id=task_id,
            expected_version=task.version,
            target=TaskStatus.READY,
        )
    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=max_parallel,
            max_parallel_tasks_per_project=max_parallel,
            max_model_concurrency=2,
            max_sandbox_concurrency=2,
        ),
        lease_ttl=timedelta(minutes=5),
    )
    claim = (await scheduler.claim_ready(owner="dispatcher:test", limit=1))[0]
    return DispatchMessage.from_claim(claim), scheduler


@pytest.mark.asyncio
async def test_dispatched_executor_completes_checkpoint_and_releases_claim(database) -> None:
    message, scheduler = await _seed_claim(database)
    node_executor = RecordingNodeExecutor()
    seen: list[TaskExecutionContext] = []

    async def factory(context: TaskExecutionContext):  # type: ignore[no-untyped-def]
        seen.append(context)
        return node_executor

    @asynccontextmanager
    async def no_checkpointer():
        yield InMemorySaver()

    executor = DispatchedTaskExecutor(
        database=database,
        scheduler=scheduler,
        node_executor_factory=factory,
        checkpointer_factory=no_checkpointer,
        production_graph=False,
        agent_spec_hash="f" * 64,
    )

    outcome = await executor.execute(message)
    replay = await executor.execute(message)

    assert outcome is WorkerOutcome.COMPLETED
    assert replay is WorkerOutcome.COMPLETED
    assert node_executor.nodes == ["recall", "inspect", "verify", "evidence"]
    assert seen[0].task_id == message.task_id
    assert "Inspect the repository" in seen[0].goal
    assert "Evidence exists" in seen[0].goal
    async with database.sessions() as session:
        task = await session.get(TaskRow, message.task_id)
        attempt = await session.get(TaskAttemptRow, message.attempt_id)
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == message.task_id)
        )
        lease = await session.scalar(select(LeaseRow).where(LeaseRow.task_id == message.task_id))
        active_reservations = tuple(
            (
                await session.scalars(
                    select(ReservationRow).where(
                        ReservationRow.task_id == message.task_id,
                        ReservationRow.released_at.is_(None),
                    )
                )
            ).all()
        )
    assert task is not None and task.state is TaskStatus.COMPLETED
    assert attempt is not None and attempt.status == "COMPLETED" and attempt.ended_at is not None
    assert graph is not None and graph.state is GraphExecutionState.COMPLETED
    assert lease is None
    assert active_reservations == ()


@pytest.mark.asyncio
async def test_dispatched_executor_rejects_a_forged_lease_token(database) -> None:
    message, scheduler = await _seed_claim(database)

    async def factory(context):  # type: ignore[no-untyped-def]
        return RecordingNodeExecutor()

    @asynccontextmanager
    async def no_checkpointer():
        yield InMemorySaver()

    executor = DispatchedTaskExecutor(
        database=database,
        scheduler=scheduler,
        node_executor_factory=factory,
        checkpointer_factory=no_checkpointer,
        production_graph=False,
        agent_spec_hash="f" * 64,
    )

    with pytest.raises(PermissionError, match="lease"):
        await executor.execute(message.model_copy(update={"lease_token": uuid4()}))


@pytest.mark.parametrize("failure_stage", ["factory", "dependency_context"])
async def test_preparation_failure_releases_the_started_task_lease(database, failure_stage) -> None:
    message, scheduler = await _seed_claim(database)

    async def factory(context):
        if failure_stage == "factory":
            raise RuntimeError("worktree preparation failed")
        return RecordingNodeExecutor()

    @asynccontextmanager
    async def no_checkpointer():
        yield InMemorySaver()

    executor = DispatchedTaskExecutor(
        database=database,
        scheduler=scheduler,
        node_executor_factory=factory,
        checkpointer_factory=no_checkpointer,
        production_graph=False,
        agent_spec_hash="f" * 64,
    )
    if failure_stage == "dependency_context":

        async def unavailable_context(_):
            raise RuntimeError("worktree preparation failed")

        executor._dependency_context = unavailable_context
    with pytest.raises(RuntimeError, match="worktree preparation failed"):
        await executor.execute(message)
    async with database.sessions() as session:
        task = await session.get(TaskRow, message.task_id)
        assert task.state is TaskStatus.FAILED
        assert (
            await session.scalar(select(LeaseRow).where(LeaseRow.task_id == message.task_id))
            is None
        )


async def test_worker_starts_and_heartbeats_every_simultaneously_leased_task(
    database, redis_client, monkeypatch
) -> None:
    pairs = [await _seed_claim(database, max_parallel=3) for _ in range(3)]
    messages = [message for message, _ in pairs]
    scheduler = pairs[-1][1]
    original_expiry = max(datetime.fromisoformat(message.expires_at) for message in messages)
    all_started = asyncio.Event()
    all_renewed = asyncio.Event()
    release = asyncio.Event()
    started = set()
    renewed = set()
    heartbeat_time = None
    original_heartbeat = scheduler.heartbeat

    async def heartbeat(*, task_id, owner, token):
        result = await original_heartbeat(
            task_id=task_id, owner=owner, token=token, now=heartbeat_time
        )
        if heartbeat_time is not None and result:
            renewed.add(task_id)
            if len(renewed) == 3:
                all_renewed.set()
        return result

    monkeypatch.setattr(scheduler, "heartbeat", heartbeat)

    async def factory(context):
        started.add(context.task_id)
        if len(started) == 3:
            all_started.set()
        await release.wait()
        return RecordingNodeExecutor()

    @asynccontextmanager
    async def checkpointer():
        yield InMemorySaver()

    executor = DispatchedTaskExecutor(
        database=database, scheduler=scheduler, node_executor_factory=factory,
        checkpointer_factory=checkpointer, production_graph=False,
        agent_spec_hash="f" * 64, heartbeat_seconds=0.02,
    )
    transport = RedisStreamsTransport(redis_client)
    inbox = RedisDispatchInbox(transport, group="lease-regression", consumer="worker-test")
    await inbox.setup()
    for message in messages:
        await transport.publish(
            "task-dispatch", message.lease_token, message.model_dump(mode="json")
        )
    stop = asyncio.Event()
    running = asyncio.create_task(WorkerService(
        inbox=inbox, executor=executor, max_concurrency=3, block_ms=1,
    ).run(stop))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=3)
        # Advance only the scheduler's injected clock. Every execution must have
        # its own heartbeat before the original 30-second-style lease expires.
        heartbeat_time = original_expiry - timedelta(seconds=1)
        await asyncio.wait_for(all_renewed.wait(), timeout=3)
        assert await scheduler.reclaim_expired(now=original_expiry + timedelta(seconds=1)) == 0
        async with database.sessions() as session:
            rows = (await session.scalars(select(TaskRow))).all()
            assert len(rows) == 3
            assert all(row.state is TaskStatus.RUNNING for row in rows)
        stop.set()
        release.set()
        await asyncio.wait_for(running, timeout=5)
        async with database.sessions() as session:
            rows = (await session.scalars(select(TaskRow))).all()
            assert all(row.state is TaskStatus.COMPLETED for row in rows)
            assert (await session.scalars(select(LeaseRow))).all() == []
        assert await redis_client.xpending_range(
            "task-dispatch", "lease-regression", min="-", max="+", count=10
        ) == []
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
