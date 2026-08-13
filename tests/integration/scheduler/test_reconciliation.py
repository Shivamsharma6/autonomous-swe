from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from domain.enums import GraphExecutionState, TaskStatus, TaskType
from domain.models import TaskSpec
from execution.scheduler.reconciliation import ReconciliationAction, ReconciliationService
from persistence.repositories import DomainRepository
from persistence.tables import AuditEventRow, GraphExecutionRow


@pytest.mark.parametrize(
    (
        "domain_state",
        "graph_state",
        "lease_expired",
        "wrong_identity",
        "expected_action",
        "expected_domain",
        "expected_graph",
    ),
    [
        (
            TaskStatus.RUNNING,
            GraphExecutionState.RUNNING,
            True,
            False,
            ReconciliationAction.RESUME_CHECKPOINT,
            TaskStatus.READY,
            GraphExecutionState.RUNNING,
        ),
        (
            TaskStatus.RUNNING,
            GraphExecutionState.COMPLETED,
            False,
            False,
            ReconciliationAction.FINALIZE_DOMAIN,
            TaskStatus.COMPLETED,
            GraphExecutionState.COMPLETED,
        ),
        (
            TaskStatus.RUNNING,
            None,
            False,
            False,
            ReconciliationAction.REQUEUE_MISSING_CHECKPOINT,
            TaskStatus.READY,
            None,
        ),
        (
            TaskStatus.COMPLETED,
            GraphExecutionState.RUNNING,
            False,
            False,
            ReconciliationAction.DOMAIN_TERMINAL_WINS,
            TaskStatus.COMPLETED,
            GraphExecutionState.CANCELLED,
        ),
        (
            TaskStatus.RUNNING,
            GraphExecutionState.RUNNING,
            False,
            True,
            ReconciliationAction.QUARANTINE,
            TaskStatus.BLOCKED,
            GraphExecutionState.NEEDS_RECONCILIATION,
        ),
        (
            TaskStatus.FAILED,
            GraphExecutionState.COMPLETED,
            False,
            False,
            ReconciliationAction.QUARANTINE,
            TaskStatus.FAILED,
            GraphExecutionState.NEEDS_RECONCILIATION,
        ),
    ],
)
@pytest.mark.asyncio
async def test_domain_checkpoint_divergence_reconciles_idempotently(
    database: object,
    domain_state: TaskStatus,
    graph_state: GraphExecutionState | None,
    lease_expired: bool,
    wrong_identity: bool,
    expected_action: ReconciliationAction,
    expected_domain: TaskStatus,
    expected_graph: GraphExecutionState | None,
) -> None:
    repository = DomainRepository()
    project_id, repository_id, run_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    task = TaskSpec(
        id=task_id,
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Reconcile state",
        description="Exercise the domain and checkpoint divergence matrix.",
        task_type=TaskType.VALIDATION,
        assigned_capability="validator",
        acceptance_criteria=("State is deterministic",),
    )
    now = datetime.now(UTC)
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Reconcile project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/reconcile.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Reconcile",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        row = await repository.create_task(session, run_id=run_id, task=task)
        row.state = domain_state
        row.version = 10
        if graph_state is not None:
            await repository.record_graph_execution(
                session,
                task_id=task_id,
                run_id=run_id,
                repository_id=repository_id,
                baseline_commit="b" * 40 if wrong_identity else "a" * 40,
                thread_id=f"run:{run_id}:task:{task_id}",
                state=graph_state,
                checkpoint_id="checkpoint-1",
            )
        if lease_expired:
            await repository.create_lease(
                session,
                task_id=task_id,
                owner="dead-worker",
                token=uuid4(),
                expires_at=now - timedelta(seconds=1),
            )
            await repository.create_reservation(
                session,
                task_id=task_id,
                project_id=project_id,
                resource="task",
                units=1,
            )

    service = ReconciliationService(database=database)
    first = await service.reconcile(project_id=project_id, task_id=task_id, now=now)
    second = await service.reconcile(project_id=project_id, task_id=task_id, now=now)

    async with database.transaction() as session:
        task_row = await repository.get_task(session, project_id=project_id, task_id=task_id)
        graph_row = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == task_id)
        )
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(AuditEventRow.event_type.like("reconciliation.%"))
        )

    assert first is expected_action
    assert second is ReconciliationAction.NOOP
    assert task_row is not None
    assert task_row.state is expected_domain
    assert (graph_row.state if graph_row else None) is expected_graph
    assert audit_count == 1
