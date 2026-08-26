from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from domain.enums import GraphExecutionState, TaskStatus
from execution.scheduler.reconciliation import ReconciliationAction, ReconciliationService
from persistence.tables import GraphExecutionRow, LeaseRow, RunRow, TaskRow


class _Identity:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.project_id = uuid4()
        self.repository_id = uuid4()
        self.baseline = "a" * 40

    def run(self) -> RunRow:
        return RunRow(
            id=self.run_id,
            project_id=self.project_id,
            repository_id=self.repository_id,
            baseline_commit=self.baseline,
        )


def _task(identity: _Identity, state: TaskStatus) -> TaskRow:
    return TaskRow(
        id=uuid4(),
        project_id=identity.project_id,
        run_id=identity.run_id,
        repository_id=identity.repository_id,
        state=state,
    )


def _graph(
    identity: _Identity, task_id: object, state: GraphExecutionState | None
) -> GraphExecutionRow | None:
    if state is None:
        return None
    return GraphExecutionRow(
        task_id=task_id,  # type: ignore[arg-type]
        run_id=identity.run_id,
        repository_id=identity.repository_id,
        baseline_commit=identity.baseline,
        state=state,
    )


def test_cancelled_graph_terminates_live_task() -> None:
    identity = _Identity()
    now = datetime.now(UTC)
    task = _task(identity, TaskStatus.RUNNING)
    action = ReconciliationService._decide(
        task, identity.run(), _graph(identity, task.id, GraphExecutionState.CANCELLED), None, now
    )
    assert action is ReconciliationAction.GRAPH_TERMINAL_WINS


def test_cancelled_graph_is_noop_for_terminal_task() -> None:
    identity = _Identity()
    now = datetime.now(UTC)
    task = _task(identity, TaskStatus.COMPLETED)
    action = ReconciliationService._decide(
        task, identity.run(), _graph(identity, task.id, GraphExecutionState.CANCELLED), None, now
    )
    assert action is ReconciliationAction.NOOP


def test_expired_lease_on_running_task_resumes() -> None:
    identity = _Identity()
    now = datetime.now(UTC)
    task = _task(identity, TaskStatus.RUNNING)
    lease = LeaseRow(
        task_id=task.id,
        owner="worker-a",
        token=uuid4(),
        expires_at=now - timedelta(seconds=1),
    )
    action = ReconciliationService._decide(
        task, identity.run(), _graph(identity, task.id, GraphExecutionState.RUNNING), lease, now
    )
    assert action is ReconciliationAction.RESUME_CHECKPOINT


def test_completed_graph_finalizes_running_task() -> None:
    identity = _Identity()
    now = datetime.now(UTC)
    task = _task(identity, TaskStatus.RUNNING)
    action = ReconciliationService._decide(
        task, identity.run(), _graph(identity, task.id, GraphExecutionState.COMPLETED), None, now
    )
    assert action is ReconciliationAction.FINALIZE_DOMAIN
