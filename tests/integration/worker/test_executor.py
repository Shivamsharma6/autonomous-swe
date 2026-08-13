from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from apps.dispatcher.main import DispatchMessage
from apps.worker.executor import DispatchedTaskExecutor, TaskExecutionContext
from apps.worker.runner import WorkerOutcome
from domain.enums import GraphExecutionState, RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, TaskSpec
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
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


async def _seed_claim(database):  # type: ignore[no-untyped-def]
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
            max_parallel_tasks=2,
            max_parallel_tasks_per_project=2,
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
