"""Reconciliation must be able to see a task the domain already settled.

Regression test for a silently permanent divergence. `reconcile_due` scanned
only tasks in RUNNING or LEASED, so the terminal branch of the reconciliation
decision function was unreachable from the periodic reconciler and reachable
only from the manual CLI. The cancellation path produces exactly this shape by
design: `cancel_requested_runs` cancels every non-terminal task of a cancelled
run, including a RUNNING one whose graph has already reached COMPLETED. The
result was `tasks.state = CANCELLED` alongside `graph_executions.state =
COMPLETED`, permanently and invisibly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from domain.enums import GraphExecutionState, TaskStatus
from execution.scheduler.reconciliation import ReconciliationService
from persistence.repositories import DomainRepository


async def seed(database: object) -> dict:
    repository = DomainRepository()
    ids = {name: uuid4() for name in (
        "project_id", "repository_id", "run_id", "task_id", "attempt_id",
    )}
    async with database.transaction() as session:
        await repository.create_project(session, project_id=ids["project_id"], name="Divergence")
        await repository.create_repository(
            session,
            repository_id=ids["repository_id"],
            project_id=ids["project_id"],
            source_path="/imports/divergence.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=ids["run_id"],
            project_id=ids["project_id"],
            repository_id=ids["repository_id"],
            goal="Diverge",
            baseline_commit="a" * 40,
        )
        from domain.enums import RiskLevel, TaskType
        from domain.models import TaskSpec

        task = TaskSpec(
            id=ids["task_id"],
            plan_revision=1,
            project_id=ids["project_id"],
            repository_id=ids["repository_id"],
            title="divergent",
            description="becomes terminal in the domain only",
            task_type=TaskType.IMPLEMENTATION,
            priority=10,
            assigned_capability="coder",
            acceptance_criteria=("done",),
            allowed_tools=("run_tests",),
            risk_ceiling=RiskLevel.MEDIUM,
        )
        await repository.create_task(session, run_id=ids["run_id"], task=task)
        await repository.create_attempt(
            session,
            attempt_id=ids["attempt_id"],
            task_id=ids["task_id"],
            agent_spec_hash="b" * 64,
        )
        await repository.record_graph_execution(
            session,
            task_id=ids["task_id"],
            run_id=ids["run_id"],
            repository_id=ids["repository_id"],
            baseline_commit="a" * 40,
            thread_id=f"run:{ids['run_id']}:task:{ids['task_id']}",
            state=GraphExecutionState.COMPLETED,
            checkpoint_id=None,
        )
    return ids


async def settle_cancelled(database: object, ids: dict) -> None:
    """The cancellation path: domain says CANCELLED, graph says COMPLETED."""
    repository = DomainRepository()
    async with database.transaction() as session:
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=1,
            target=TaskStatus.READY,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=2,
            target=TaskStatus.LEASED,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=3,
            target=TaskStatus.RUNNING,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=4,
            target=TaskStatus.CANCELLED,
        )


async def test_a_terminal_task_with_a_non_terminal_graph_is_examined(
    database: object,
) -> None:
    ids = await seed(database)
    await settle_cancelled(database, ids)
    service = ReconciliationService(database=database)

    results = await service.reconcile_due(now=datetime.now(UTC), limit=32)

    assert ids["task_id"] in results, (
        "a CANCELLED task whose graph reached COMPLETED must be surfaced, not ignored"
    )


async def test_the_divergence_is_classified_not_silently_accepted(
    database: object,
) -> None:
    ids = await seed(database)
    await settle_cancelled(database, ids)
    service = ReconciliationService(database=database)

    results = await service.reconcile_due(now=datetime.now(UTC), limit=32)

    action = results[ids["task_id"]]
    assert action.name != "NOOP", "the disagreement must produce a decision"


async def test_reconciling_a_fully_agreed_terminal_pair_is_a_noop(
    database: object,
) -> None:
    """Widening the scan must not disturb the settled-and-consistent case."""
    ids = await seed(database)
    repository = DomainRepository()
    async with database.transaction() as session:
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=1,
            target=TaskStatus.READY,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=2,
            target=TaskStatus.LEASED,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=3,
            target=TaskStatus.RUNNING,
        )
        await repository.transition_task(
            session,
            project_id=ids["project_id"],
            task_id=ids["task_id"],
            expected_version=4,
            target=TaskStatus.COMPLETED,
        )
    service = ReconciliationService(database=database)

    results = await service.reconcile_due(now=datetime.now(UTC), limit=32)

    entry = results.get(ids["task_id"])
    assert entry is None or entry.name == "NOOP"


async def test_the_scan_is_bounded(database: object) -> None:
    service = ReconciliationService(database=database)

    results = await service.reconcile_due(now=datetime.now(UTC), limit=1)

    assert len(results) <= 1
