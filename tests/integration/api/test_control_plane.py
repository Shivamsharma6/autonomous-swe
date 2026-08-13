from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.dependencies import ControlPlaneServices, ReadinessChecks
from apps.api.main import create_app
from apps.api.websocket import PostgresTaskEventSource
from domain.enums import RiskLevel, TaskType
from domain.models import TaskSpec, ToolCallRequest
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from infrastructure.config import Settings
from knowledge.memory.fake import FakeMemoryPort
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import DeadLetterRow, OutboxRow
from tools.approval import ApprovalService
from tools.registry import ToolExecutionContext

ADMIN_TOKEN = "z" * 40


class ReadyRedis:
    async def ping(self) -> bool:
        return True


def settings() -> Settings:
    return Settings.model_validate(
        {
            "autoswe_env": "test",
            "admin_token": ADMIN_TOKEN,
            "database_url": "test://postgres",
            "redis_url": "test://redis",
            "uams_url": "test://uams",
            "model_base_url": "test://model",
            "model_primary": "scripted-model",
            "cors_origins": ["https://console.example"],
            "python_runner_image": "test://python",
            "node_runner_image": "test://node",
            "api_rate_limit_per_minute": 1_000,
        }
    )


async def probe() -> bool:
    return True


def services(database: Database, tmp_path: Path) -> ControlPlaneServices:
    repository = DomainRepository()
    return ControlPlaneServices(
        settings=settings(),
        database=database,
        redis=ReadyRedis(),
        memory=FakeMemoryPort(),
        approvals=ApprovalService(database=database),
        artifacts=ArtifactService(
            store=ArtifactStore(tmp_path / "artifacts"),
            repository=repository,
        ),
        scheduler=SchedulerService(
            database=database,
            policy=ConcurrencyPolicy(
                max_parallel_tasks=4,
                max_parallel_tasks_per_project=4,
                max_model_concurrency=2,
                max_sandbox_concurrency=2,
            ),
            lease_ttl=timedelta(seconds=30),
        ),
        cancel_notify=_notify,
        readiness=ReadinessChecks(
            postgres=probe,
            redis=probe,
            checkpoints=probe,
            sandbox=probe,
            model=probe,
            uams=probe,
        ),
        database_repository=repository,
    )


async def _notify(_: object) -> None:
    return None


def authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.mark.asyncio
async def test_control_plane_scopes_state_artifacts_approval_and_dead_letter_replay(
    database: Database,
    tmp_path: Path,
) -> None:
    dependencies = services(database, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dependencies)),
        base_url="http://control-plane.test",
        headers=authorization(),
    ) as client:
        project = await client.post(
            "/api/v1/projects",
            json={"name": "SaaS", "source_path": "/imports/saas.git"},
        )
        assert project.status_code == 201
        project_id = UUID(project.json()["project_id"])
        repository_id = UUID(project.json()["repository_id"])
        run = await client.post(
            "/api/v1/runs",
            json={
                "project_id": str(project_id),
                "repository_id": str(repository_id),
                "goal": "Build a secure session API",
                "baseline_commit": "a" * 40,
            },
        )
        assert run.status_code == 202
        run_id = UUID(run.json()["run_id"])

        task = TaskSpec(
            id=uuid4(),
            plan_revision=1,
            project_id=project_id,
            repository_id=repository_id,
            title="Implement sessions",
            description="Implement and verify sessions.",
            task_type=TaskType.IMPLEMENTATION,
            assigned_capability="coder",
            acceptance_criteria=("Session tests pass",),
            risk_ceiling=RiskLevel.HIGH,
        )
        attempt_id = uuid4()
        async with database.transaction() as session:
            await dependencies.database_repository.create_plan_revision(
                session,
                run_id=run_id,
                revision=1,
                plan={},
            )
            await dependencies.database_repository.create_task(session, run_id=run_id, task=task)
            await dependencies.database_repository.create_attempt(
                session,
                attempt_id=attempt_id,
                task_id=task.id,
                agent_spec_hash="b" * 64,
            )
            stored = await dependencies.artifacts.put(
                session,
                content=b"verified evidence",
                media_type="text/plain",
                project_id=task.project_id,
                run_id=run_id,
                task_id=task.id,
            )
            dead = DeadLetterRow(
                event_id=uuid4(),
                consumer="worker-1",
                topic="task-dispatch",
                payload={"task_id": str(task.id)},
                attempts=8,
                last_error="transient downstream failure",
                causation_chain=[],
            )
            session.add(dead)
            await session.flush()
            dead_letter_id = dead.id

        task_response = await client.get(
            f"/api/v1/projects/{project_id}/tasks/{task.id}"
        )
        invalid_id = await client.get(f"/api/v1/projects/{project_id}/tasks/not-a-uuid")
        wrong_scope = await client.get(
            f"/api/v1/projects/{uuid4()}/artifacts/{stored.artifact_id}"
        )
        artifact = await client.get(
            f"/api/v1/projects/{project_id}/artifacts/{stored.artifact_id}"
        )

        assert task_response.status_code == 200
        assert task_response.json()["task_id"] == str(task.id)
        assert invalid_id.status_code == 422
        assert wrong_scope.status_code == 404
        assert artifact.status_code == 200
        assert artifact.content == b"verified evidence"

        tool_call = ToolCallRequest(
            call_id=uuid4(),
            run_id=run_id,
            task_id=task.id,
            attempt_id=attempt_id,
            requested_by="coder",
            tool_name="git_commit",
            tool_version="1.0",
            arguments={"schema_version": "1.0", "message": "verified"},
            idempotency_key=f"commit:{task.id}",
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        approval = await dependencies.approvals.request(
            tool_call,
            context=ToolExecutionContext(
                project_id=task.project_id,
                repository_id=task.repository_id,
                run_id=run_id,
                task_id=task.id,
                attempt_id=attempt_id,
                baseline_commit="a" * 40,
                agent_role="coder",
                agent_capabilities=frozenset({"repository.commit"}),
                risk_ceiling=RiskLevel.HIGH,
                worktree_root=worktree,
            ),
        )
        mismatch = await client.post(
            f"/api/v1/approvals/{approval.approval_id}/decision",
            json={
                "approved": True,
                "approver": "admin@example.com",
                "expected_call_hash": "f" * 64,
            },
        )
        approved = await client.post(
            f"/api/v1/approvals/{approval.approval_id}/decision",
            json={
                "approved": True,
                "approver": "admin@example.com",
                "expected_call_hash": approval.call_hash,
            },
        )
        assert mismatch.status_code == 409
        assert approved.status_code == 202

        dead_letters = await client.get("/api/v1/dead-letters")
        replayed = await client.post(f"/api/v1/dead-letters/{dead_letter_id}/replay")
        assert dead_letters.status_code == 200
        assert "payload" not in dead_letters.text
        assert replayed.status_code == 202

        cancelled = await client.post(
            f"/api/v1/projects/{project_id}/tasks/{task.id}/cancel"
        )
        assert cancelled.status_code == 202
        events = await PostgresTaskEventSource(database, poll_seconds=0).next_events(
            task.id,
            after=None,
        )
        assert events
        assert all(event["task_id"] == str(task.id) for event in events)

    async with database.sessions() as session:
        replay_count = await session.scalar(
            select(func.count()).select_from(OutboxRow).where(OutboxRow.topic == "task-dispatch")
        )
        assert replay_count == 1


@pytest.mark.asyncio
async def test_readiness_is_dependency_aware(
    database: Database,
    tmp_path: Path,
) -> None:
    dependencies = services(database, tmp_path)

    async def unavailable() -> bool:
        return False

    dependencies.readiness = ReadinessChecks(
        postgres=probe,
        redis=probe,
        checkpoints=probe,
        sandbox=probe,
        model=probe,
        uams=unavailable,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dependencies)),
        base_url="http://control-plane.test",
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["uams"] is False
