from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from domain.enums import GraphExecutionState, RiskLevel, TaskStatus, TaskType
from domain.messages import ContextHandoff
from domain.models import (
    ApprovalRequest,
    ArtifactRef,
    BudgetPolicy,
    MemoryCandidate,
    SandboxExecution,
    TaskSpec,
    ToolCallRequest,
)
from persistence.repositories import DomainRepository, StaleStateError
from persistence.tables import AuditEventRow, OutboxRow


def make_task(project_id: object, repository_id: object) -> TaskSpec:
    return TaskSpec(
        id=uuid4(),
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Implement session endpoint",
        description="Implement the typed session endpoint and verify it.",
        task_type=TaskType.IMPLEMENTATION,
        assigned_capability="coder",
        acceptance_criteria=("Targeted tests pass",),
        allowed_tools=("read_file", "apply_patch", "run_tests"),
        risk_ceiling=RiskLevel.MEDIUM,
        budget=BudgetPolicy(cost_usd=2, wall_time_seconds=600),
    )


async def create_core(repository: DomainRepository, session: object) -> dict[str, object]:
    project_id = uuid4()
    repository_id = uuid4()
    run_id = uuid4()
    task = make_task(project_id, repository_id)
    await repository.create_project(session, project_id=project_id, name="SaaS project")
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
        goal="Add authenticated sessions",
        baseline_commit="a" * 40,
    )
    await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={"tasks": 1})
    await repository.create_task(session, run_id=run_id, task=task)
    return {
        "project_id": project_id,
        "repository_id": repository_id,
        "run_id": run_id,
        "task": task,
    }


@pytest.mark.asyncio
async def test_persists_every_authoritative_record_family(database: object) -> None:
    repository = DomainRepository()
    now = datetime.now(UTC)
    async with database.transaction() as session:
        core = await create_core(repository, session)
        task = core["task"]
        attempt_id = uuid4()
        message = ContextHandoff(
            sender="researcher",
            recipient="coder",
            run_id=core["run_id"],
            task_id=task.id,
            attempt_id=attempt_id,
            created_at=now,
            causation_id=uuid4(),
            correlation_id=core["run_id"],
            summary="Relevant source files identified",
            context_ids=(uuid4(),),
        )
        artifact = ArtifactRef(
            artifact_id=uuid4(),
            sha256="b" * 64,
            media_type="application/json",
        )
        tool_call = ToolCallRequest(
            call_id=uuid4(),
            run_id=core["run_id"],
            task_id=task.id,
            attempt_id=attempt_id,
            requested_by="coder",
            tool_name="run_tests",
            arguments={"target": "tests/test_session.py"},
            idempotency_key="run-tests:attempt-1",
        )
        approval = ApprovalRequest(
            approval_id=uuid4(),
            call=tool_call,
            project_id=core["project_id"],
            repository_id=core["repository_id"],
            baseline_commit="a" * 40,
            expires_at=now + timedelta(hours=1),
        )
        memory = MemoryCandidate(
            candidate_id=uuid4(),
            project_id=core["project_id"],
            source_run_id=core["run_id"],
            source_task_id=task.id,
            source_attempt_id=attempt_id,
            source_agent="reviewer",
            classification="procedural",
            content="Run targeted tests before full verification.",
            observed_at=now,
            verified_at=now,
            repository_id=core["repository_id"],
            baseline_commit="a" * 40,
            originating_message_ids=(message.message_id,),
            artifact_hashes=(artifact.sha256,),
            verification_commands=(("python", "-m", "pytest", "-q"),),
            confidence=0.9,
        )
        sandbox = SandboxExecution(
            execution_id=uuid4(),
            task_id=task.id,
            cpu_time_ms=10,
            peak_memory_bytes=1_024,
            peak_processes=2,
            processes_created=2,
            stdout_bytes=20,
            stderr_bytes=0,
            duration_ms=50,
            network_requests=0,
            network_bytes_sent=0,
            network_bytes_received=0,
            exit_code=0,
            exit_reason="COMPLETED",
            limit_triggered=None,
            measurement_source="docker-cgroups-v2",
            measurement_complete=True,
        )

        await repository.create_attempt(
            session,
            attempt_id=attempt_id,
            task_id=task.id,
            agent_spec_hash="c" * 64,
        )
        await repository.create_lease(
            session,
            task_id=task.id,
            owner="dispatcher-1",
            token=uuid4(),
            expires_at=now + timedelta(seconds=30),
        )
        await repository.create_reservation(
            session,
            task_id=task.id,
            project_id=core["project_id"],
            resource="sandbox",
            units=1,
        )
        graph_execution = await repository.record_graph_execution(
            session,
            task_id=task.id,
            run_id=core["run_id"],
            repository_id=core["repository_id"],
            baseline_commit="a" * 40,
            thread_id=f"run:{core['run_id']}:task:{task.id}",
            state=GraphExecutionState.RUNNING,
            checkpoint_id="checkpoint-1",
        )
        assert graph_execution.run_id == core["run_id"]
        assert graph_execution.repository_id == core["repository_id"]
        assert graph_execution.baseline_commit == "a" * 40
        await repository.persist_message(session, message)
        await repository.enqueue_event(
            session,
            event_id=message.message_id,
            topic="agent-messages",
            payload=message.model_dump(mode="json"),
        )
        assert await repository.claim_consumer_receipt(
            session, consumer="worker-1", event_id=message.message_id
        )
        await repository.record_dead_letter(
            session,
            event_id=uuid4(),
            topic="agent-messages",
            payload={"kind": "blocker"},
            attempts=8,
            last_error="consumer unavailable",
            causation_chain=(message.message_id,),
        )
        await repository.record_tool_execution(
            session,
            call=tool_call,
            status="COMPLETED",
            result={"exit_code": 0},
        )
        await repository.create_approval(session, approval)
        await repository.record_artifact(
            session,
            artifact=artifact,
            project_id=core["project_id"],
            run_id=core["run_id"],
            task_id=task.id,
            storage_key=f"sha256/{artifact.sha256}",
            size_bytes=2,
        )
        await repository.create_memory_candidate(session, memory)
        await repository.record_sandbox_execution(session, sandbox, attempt_id=attempt_id)
        await repository.record_state_duration(
            session,
            aggregate_type="task",
            aggregate_id=task.id,
            state=TaskStatus.RUNNING.value,
            entered_at=now,
            exited_at=now + timedelta(seconds=5),
        )
        await repository.append_audit(
            session,
            event_id=uuid4(),
            event_type="integration.recorded",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={"verified": True},
            correlation_id=core["run_id"],
            causation_id=message.message_id,
        )

    async with database.transaction() as session:
        counts = await repository.record_counts(session)

    expected = {
        "projects",
        "repositories",
        "runs",
        "plan_revisions",
        "tasks",
        "task_attempts",
        "leases",
        "reservations",
        "graph_executions",
        "agent_messages",
        "outbox",
        "consumer_receipts",
        "dead_letters",
        "tool_executions",
        "approvals",
        "artifacts",
        "memory_candidates",
        "sandbox_executions",
        "state_durations",
        "audit_events",
    }
    assert expected <= {name for name, count in counts.items() if count >= 1}


@pytest.mark.asyncio
async def test_task_transition_is_compare_and_set_and_terminal_safe(database: object) -> None:
    repository = DomainRepository()
    async with database.transaction() as session:
        core = await create_core(repository, session)
        task = core["task"]

    async with database.transaction() as session:
        ready = await repository.transition_task(
            session,
            project_id=core["project_id"],
            task_id=task.id,
            expected_version=1,
            target=TaskStatus.READY,
        )
        assert ready.version == 2

    async with database.transaction() as session:
        with pytest.raises(StaleStateError):
            await repository.transition_task(
                session,
                project_id=core["project_id"],
                task_id=task.id,
                expected_version=1,
                target=TaskStatus.LEASED,
            )

    async with database.transaction() as session:
        await repository.transition_task(
            session,
            project_id=core["project_id"],
            task_id=task.id,
            expected_version=2,
            target=TaskStatus.FAILED,
        )

    async with database.transaction() as session:
        with pytest.raises(ValueError, match="illegal task transition"):
            await repository.transition_task(
                session,
                project_id=core["project_id"],
                task_id=task.id,
                expected_version=3,
                target=TaskStatus.READY,
            )


@pytest.mark.asyncio
async def test_transition_audit_and_outbox_roll_back_atomically(database: object) -> None:
    repository = DomainRepository()
    async with database.transaction() as session:
        core = await create_core(repository, session)
        task = core["task"]

    with pytest.raises(RuntimeError, match="force rollback"):
        async with database.transaction() as session:
            await repository.transition_task(
                session,
                project_id=core["project_id"],
                task_id=task.id,
                expected_version=1,
                target=TaskStatus.READY,
            )
            raise RuntimeError("force rollback")

    async with database.transaction() as session:
        persisted = await repository.get_task(
            session, project_id=core["project_id"], task_id=task.id
        )
        audit_count = await session.scalar(select(func.count()).select_from(AuditEventRow))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxRow))

    assert persisted is not None
    assert persisted.state == TaskStatus.PENDING
    assert persisted.version == 1
    assert audit_count == 0
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_task_reads_are_always_project_scoped(database: object) -> None:
    repository = DomainRepository()
    async with database.transaction() as session:
        core = await create_core(repository, session)
        task = core["task"]

    async with database.transaction() as session:
        assert await repository.get_task(session, project_id=uuid4(), task_id=task.id) is None
        assert (
            await repository.get_task(
                session,
                project_id=core["project_id"],
                task_id=task.id,
            )
            is not None
        )
