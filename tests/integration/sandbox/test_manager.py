from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from domain.enums import RiskLevel, TaskType
from domain.models import SandboxExecution, TaskSpec
from execution.repositories import CommandSpec
from execution.sandbox.manager import (
    PostgresSandboxRunStore,
    SandboxManager,
    SandboxRunStatus,
)
from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import SandboxRequest, SandboxResult
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import SandboxExecutionRow


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.cancelled: list[UUID] = []
        self.killed: list[str] = []
        self.removed: list[str] = []
        self.container = "c" * 64

    def create(self, request: SandboxRequest) -> str:
        return self.container

    def run_created(self, request: SandboxRequest, container_id: str) -> SandboxResult:
        assert container_id == self.container
        self.started.set()
        if not self.unblock.wait(timeout=5):
            raise TimeoutError("test runner was not released")
        return SandboxResult(
            execution=SandboxExecution(
                execution_id=request.execution_id,
                task_id=request.task_id,
                cpu_time_ms=10,
                peak_memory_bytes=1_024,
                peak_processes=1,
                processes_created=None,
                stdout_bytes=3,
                stderr_bytes=0,
                duration_ms=20,
                network_requests=0,
                network_bytes_sent=0,
                network_bytes_received=0,
                exit_code=0,
                exit_reason="COMPLETED",
                limit_triggered=None,
                measurement_source="test",
                measurement_complete=True,
            ),
            stdout="ok\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def cancel(self, execution_id: UUID) -> bool:
        self.cancelled.append(execution_id)
        self.unblock.set()
        return True

    def kill_container(self, container_id: str) -> None:
        self.killed.append(container_id)
        self.unblock.set()

    def remove_container(self, container_id: str) -> None:
        self.removed.append(container_id)

    def release(self, execution_id: UUID, container_id: str, *, remove: bool = True) -> None:
        if remove:
            self.remove_container(container_id)


async def sandbox_request(database: Database, tmp_path: Path) -> SandboxRequest:
    repository = DomainRepository()
    project_id = uuid4()
    repository_id = uuid4()
    run_id = uuid4()
    task = TaskSpec(
        id=uuid4(),
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Run isolated verification",
        description="Execute the governed repository verification command.",
        task_type=TaskType.TEST,
        assigned_capability="tester",
        acceptance_criteria=("Tests complete",),
        risk_ceiling=RiskLevel.MEDIUM,
    )
    attempt_id = uuid4()
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Sandbox project")
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
            goal="Verify safely",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        await repository.create_task(session, run_id=run_id, task=task)
        await repository.create_attempt(
            session,
            attempt_id=attempt_id,
            task_id=task.id,
            agent_spec_hash="b" * 64,
        )
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    return SandboxRequest(
        execution_id=uuid4(),
        run_id=run_id,
        task_id=task.id,
        attempt_id=attempt_id,
        source_repository=source,
        worktree=worktree,
        command=CommandSpec(argv=("python", "-V"), timeout_seconds=10),
        policy=SandboxPolicy(
            image="registry.example/runner@sha256:" + "d" * 64,
            uid=65532,
            gid=65532,
            cpu_nanos=500_000_000,
            cpu_time_limit_ms=30_000,
            memory_bytes=128 * 1024 * 1024,
            pids_limit=32,
            timeout_seconds=30,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
            max_total_output_bytes=2_048,
        ),
    )


@pytest.mark.asyncio
async def test_container_id_is_durable_before_start_and_cancellation_propagates(
    database: Database,
    tmp_path: Path,
) -> None:
    request = await sandbox_request(database, tmp_path)
    runner = BlockingRunner()
    store = PostgresSandboxRunStore(database)
    manager = SandboxManager(runner, store)

    execution = asyncio.create_task(manager.execute(request))
    assert await asyncio.to_thread(runner.started.wait, 2)
    running = await store.get(request.execution_id)

    assert running is not None
    assert running.container_id == runner.container
    assert running.status is SandboxRunStatus.RUNNING
    assert await manager.cancel(request.execution_id) is True
    result = await execution

    assert result.execution.exit_reason == "CANCELLED"
    assert result.execution.limit_triggered == "cancellation"
    final = await store.get(request.execution_id)
    assert final is not None
    assert final.status is SandboxRunStatus.FINISHED
    assert final.cancellation_requested is True
    assert runner.cancelled == [request.execution_id]
    assert runner.removed == [runner.container]
    async with database.sessions() as session:
        persisted = await session.get(SandboxExecutionRow, request.execution_id)
        assert persisted is not None
        assert persisted.exit_reason == "CANCELLED"


@pytest.mark.asyncio
async def test_worker_restart_reconciliation_terminates_durable_orphans(
    database: Database,
    tmp_path: Path,
) -> None:
    request = await sandbox_request(database, tmp_path)
    runner = BlockingRunner()
    store = PostgresSandboxRunStore(database)
    await store.register(request, runner.container)
    await store.mark_running(request.execution_id)

    reconciled = await SandboxManager(runner, store).reconcile_after_worker_restart()

    assert reconciled == 1
    assert runner.killed == [runner.container]
    assert runner.removed == [runner.container]
    row = await store.get(request.execution_id)
    assert row is not None
    assert row.status is SandboxRunStatus.INTERRUPTED

