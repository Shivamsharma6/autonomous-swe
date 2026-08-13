from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from domain.enums import GraphExecutionState, TaskStatus
from execution.scheduler.reconciliation import ReconciliationAction, ReconciliationService
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, LeaseRow, ReservationRow, TaskRow
from tests.integration.messaging.helpers import seed_task


@pytest.mark.parametrize(
    ("graph_state", "expired", "expected_action", "expected_task"),
    [
        (
            GraphExecutionState.RUNNING,
            True,
            ReconciliationAction.RESUME_CHECKPOINT,
            TaskStatus.READY,
        ),
        (
            GraphExecutionState.COMPLETED,
            False,
            ReconciliationAction.FINALIZE_DOMAIN,
            TaskStatus.COMPLETED,
        ),
    ],
)
@pytest.mark.asyncio
async def test_worker_death_reconciles_checkpoint_and_domain_exactly_once(
    database: Any,
    graph_state: GraphExecutionState,
    expired: bool,
    expected_action: ReconciliationAction,
    expected_task: TaskStatus,
) -> None:
    ids = await seed_task(database, run_state="RUNNING")
    repository = DomainRepository()
    now = datetime.now(UTC)
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
        task.version = 3
        await repository.record_graph_execution(
            session,
            task_id=ids["task_id"],
            run_id=ids["run_id"],
            repository_id=ids["repository_id"],
            baseline_commit="b" * 40,
            thread_id=f"run:{ids['run_id']}:task:{ids['task_id']}",
            state=graph_state,
            checkpoint_id="durable-checkpoint",
        )
        await repository.create_lease(
            session,
            task_id=ids["task_id"],
            owner="dead-worker",
            token=uuid4(),
            expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=1),
        )
        await repository.create_reservation(
            session,
            task_id=ids["task_id"],
            project_id=ids["project_id"],
            resource="task",
            units=1,
        )

    reconciler = ReconciliationService(database=database)
    assert await reconciler.reconcile(
        project_id=ids["project_id"], task_id=ids["task_id"], now=now
    ) is expected_action
    assert await reconciler.reconcile(
        project_id=ids["project_id"], task_id=ids["task_id"], now=now
    ) is ReconciliationAction.NOOP

    async with database.sessions() as session:
        task = await session.get(TaskRow, ids["task_id"])
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
        lease = await session.scalar(
            select(LeaseRow).where(LeaseRow.task_id == ids["task_id"])
        )
        active_reservations = tuple(
            (
                await session.scalars(
                    select(ReservationRow).where(
                        ReservationRow.task_id == ids["task_id"],
                        ReservationRow.released_at.is_(None),
                    )
                )
            ).all()
        )
    assert task is not None and task.state is expected_task
    assert graph is not None and graph.checkpoint_id == "durable-checkpoint"
    if expired:
        assert lease is None
        assert active_reservations == ()
