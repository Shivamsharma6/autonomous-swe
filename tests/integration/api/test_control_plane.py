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
from persistence.tables import DeadLetterRow, OutboxRow, utc_now
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
    import_root = tmp_path / "imports"
    import_root.mkdir(exist_ok=True)
    configured = settings().model_copy(update={"repository_import_root": import_root})
    return ControlPlaneServices(
        settings=configured,
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


async def test_saved_model_settings_survive_a_new_api_instance(database, tmp_path):
    first = services(database, tmp_path)
    payload = {
        "base_url": "http://ollama.test:11434/v1",
        "primary_model": "selected-local-model",
        "fallback_models": ["local-fallback"],
        "timeout_seconds": 187,
        "temperature": 0.4,
        "api_key": "private-provider-key-for-this-test",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(first)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        saved = await client.post("/api/v1/models/config", json=payload)
        assert saved.status_code == 200
        assert payload["api_key"] not in saved.text
    second = services(database, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(second)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        response = await client.get("/api/v1/models/config")
        assert response.status_code == 200
        for field in (
            "base_url", "primary_model", "fallback_models", "timeout_seconds", "temperature",
        ):
            assert response.json()[field] == payload[field]
        assert response.json()["has_api_key"] is True
        assert payload["api_key"] not in response.text


async def test_new_run_keeps_its_model_snapshot_when_workspace_settings_change(database, tmp_path):
    from persistence.tables import RunRow

    configured = services(database, tmp_path)
    repo = configured.settings.repository_import_root / "snapshot-test"
    repo.mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        project = (await client.post("/api/v1/projects", json={
            "name": "Model snapshot", "source_path": str(repo),
        })).json()
        old = {"base_url": "http://first-model.test/v1", "primary_model": "first-model",
               "timeout_seconds": 92, "temperature": 0.3, "fallback_models": ["backup"]}
        assert (await client.post("/api/v1/models/config", json=old)).status_code == 200
        started = await client.post("/api/v1/runs", json={
            **project, "goal": "Test run configuration", "baseline_commit": "a" * 40,
        })
        assert started.status_code == 202
        run_id = UUID(started.json()["run_id"])
        assert (await client.post("/api/v1/models/config", json={
            "base_url": "http://second-model.test/v1", "primary_model": "second-model",
        })).status_code == 200
        async with database.sessions() as session:
            row = await session.get(RunRow, run_id)
            snapshot = getattr(row, "model_configuration", None)
            assert snapshot is not None, "new runs must capture durable model configuration"
            for field, value in old.items():
                assert snapshot[field] == value
        public_run = await client.get(f"/api/v1/runs/{run_id}")
        assert "model_configuration" not in public_run.json()


async def test_onboard_missing_repository_does_not_scaffold_or_create_records(database, tmp_path):
    from persistence.tables import ProjectRow

    configured = services(database, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        response = await client.post("/api/v1/projects/onboard", json={
            "name": "Missing", "folder_name": "not-present", "default_branch": "main",
        })
    assert response.status_code == 422
    assert not (configured.settings.repository_import_root / "not-present").exists()
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 0


@pytest.mark.parametrize(
    "bad_path", ["../escape.py", "/absolute.py", ".git/config", "a/../../escape.py"],
)
async def test_onboard_invalid_uploaded_paths_reject_entire_import(database, tmp_path, bad_path):
    configured = services(database, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        response = await client.post("/api/v1/projects/onboard", json={
            "name": "Import", "folder_name": "uploaded", "default_branch": "main",
            "files": [{"path": "README.md", "content": "Safe"},
                      {"path": bad_path, "content": "Untrusted"}],
        })
    assert response.status_code == 422
    assert not (configured.settings.repository_import_root / "uploaded").exists()


async def test_onboard_upload_cannot_overwrite_existing_repository(database, tmp_path):
    configured = services(database, tmp_path)
    target = configured.settings.repository_import_root / "existing"
    target.mkdir()
    (target / "README.md").write_text("Original")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)), base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as client:
        response = await client.post("/api/v1/projects/onboard", json={
            "name": "Import", "folder_name": "existing", "default_branch": "main",
            "files": [{"path": "README.md", "content": "Overwritten"}],
        })
    assert response.status_code == 409
    assert (target / "README.md").read_text() == "Original"


def authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.mark.asyncio
async def test_control_plane_scopes_state_artifacts_approval_and_dead_letter_replay(
    database: Database,
    tmp_path: Path,
) -> None:
    dependencies = services(database, tmp_path)
    imported_repository = dependencies.settings.repository_import_root / "saas"
    imported_repository.mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dependencies)),
        base_url="http://control-plane.test",
        headers=authorization(),
    ) as client:
        project = await client.post(
            "/api/v1/projects",
            json={"name": "SaaS", "source_path": str(imported_repository)},
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

        run_status = await client.get(f"/api/v1/runs/{run_id}")
        run_tasks = await client.get(f"/api/v1/runs/{run_id}/tasks")
        run_approvals = await client.get(f"/api/v1/runs/{run_id}/approvals")
        run_artifacts = await client.get(f"/api/v1/runs/{run_id}/artifacts")
        run_events = await client.get(f"/api/v1/runs/{run_id}/events")
        assert run_status.status_code == 200
        assert run_status.json()["run_id"] == str(run_id)
        assert run_status.json()["active_plan_revision"] == 1
        assert run_tasks.status_code == 200
        assert run_tasks.json()[0]["dependencies"] == []
        assert run_approvals.status_code == 200
        assert run_approvals.json()[0]["call_hash"] == approval.call_hash
        assert run_artifacts.status_code == 200
        assert run_artifacts.json()[0]["sha256"] == stored.sha256
        assert run_events.status_code == 200
        assert any(event["event_type"] == "run.requested" for event in run_events.json())

        escaped = await client.post(
            "/api/v1/projects",
            json={"name": "Escaped", "source_path": str(tmp_path)},
        )
        assert escaped.status_code == 422

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


@pytest.mark.asyncio
async def test_run_listing_and_task_message_feeds(
    database: Database,
    tmp_path: Path,
) -> None:
    dependencies = services(database, tmp_path)
    imported_repository = dependencies.settings.repository_import_root / "listrepo"
    imported_repository.mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dependencies)),
        base_url="http://control-plane.test",
        headers=authorization(),
    ) as client:
        project = await client.post(
            "/api/v1/projects",
            json={"name": "Listed", "source_path": str(imported_repository)},
        )
        assert project.status_code == 201
        project_id = UUID(project.json()["project_id"])
        repository_id = UUID(project.json()["repository_id"])

        created: list[UUID] = []
        for index in range(3):
            response = await client.post(
                "/api/v1/runs",
                json={
                    "project_id": str(project_id),
                    "repository_id": str(repository_id),
                    "goal": f"Run number {index}",
                    "baseline_commit": "a" * 40,
                },
            )
            assert response.status_code == 202
            created.append(UUID(response.json()["run_id"]))

        listing = await client.get("/api/v1/runs")
        assert listing.status_code == 200
        rows = listing.json()
        assert [row["run_id"] for row in rows] == [str(value) for value in reversed(created)]
        assert all(row["project_name"] == "Listed" for row in rows)

        filtered = await client.get(
            "/api/v1/runs",
            params={"status": "PENDING", "project_id": str(project_id)},
        )
        assert filtered.status_code == 200
        assert len(filtered.json()) == 3

        bad_status = await client.get("/api/v1/runs", params={"status": "WAT"})
        assert bad_status.status_code == 422

        # Task message feed: seed two handoffs and read them newest-first.
        task = TaskSpec(
            id=uuid4(),
            plan_revision=1,
            project_id=project_id,
            repository_id=repository_id,
            title="Summarize work",
            description="Produce handoff summaries.",
            task_type=TaskType.RESEARCH,
            assigned_capability="researcher",
            acceptance_criteria=("Summary recorded",),
            risk_ceiling=RiskLevel.LOW,
        )
        attempt_a, attempt_b = uuid4(), uuid4()
        async with database.transaction() as session:
            await dependencies.database_repository.create_plan_revision(
                session, run_id=created[0], revision=1, plan={}
            )
            await dependencies.database_repository.create_task(
                session, run_id=created[0], task=task
            )
            await dependencies.database_repository.create_attempt(
                session,
                attempt_id=attempt_a,
                task_id=task.id,
                agent_spec_hash="c" * 64,
            )
            await dependencies.database_repository.create_attempt(
                session,
                attempt_id=attempt_b,
                task_id=task.id,
                agent_spec_hash="c" * 64,
            )
            from domain.messages import ContextHandoff

            for attempt, text in (
                (attempt_a, "older recall summary"),
                (attempt_b, "newest recall summary"),
            ):
                await dependencies.database_repository.persist_message(
                    session,
                    ContextHandoff(
                        message_id=uuid4(),
                        sender="researcher",
                        recipient="workflow",
                        run_id=created[0],
                        task_id=task.id,
                        attempt_id=attempt,
                        created_at=utc_now(),
                        causation_id=uuid4(),
                        correlation_id=created[0],
                        artifact_ids=(),
                        summary=text,
                    ),
                )

        feed = await client.get(
            f"/api/v1/projects/{project_id}/tasks/{task.id}/messages"
        )
        assert feed.status_code == 200
        messages = feed.json()
        assert [message["summary"] for message in messages] == [
            "newest recall summary",
            "older recall summary",
        ]
        missing = await client.get(
            f"/api/v1/projects/{project_id}/tasks/{uuid4()}/messages"
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_run_history_returns_model_call_samples(
    database: Database,
    tmp_path: Path,
) -> None:
    dependencies = services(database, tmp_path)
    imported_repository = dependencies.settings.repository_import_root / "historyrepo"
    imported_repository.mkdir()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(dependencies)),
        base_url="http://control-plane.test",
        headers=authorization(),
    ) as client:
        project = await client.post(
            "/api/v1/projects",
            json={"name": "HistoryProj", "source_path": str(imported_repository)},
        )
        assert project.status_code == 201
        project_id = UUID(project.json()["project_id"])
        repository_id = UUID(project.json()["repository_id"])

        run_resp = await client.post(
            "/api/v1/runs",
            json={
                "project_id": str(project_id),
                "repository_id": str(repository_id),
                "goal": "History test run",
                "baseline_commit": "b" * 40,
            },
        )
        assert run_resp.status_code == 202
        run_id = UUID(run_resp.json()["run_id"])

        # Seed two model calls via run stage attempt scope
        from persistence.tables import ModelCallRow, RunStageAttemptRow

        stage_attempt_id = uuid4()
        async with database.transaction() as session:
            session.add(
                RunStageAttemptRow(
                    id=stage_attempt_id,
                    run_id=run_id,
                    stage="architect",
                    agent_spec_hash="a" * 64,
                    status="COMPLETED",
                )
            )
            await session.flush()
            for idx in range(2):
                row = ModelCallRow(
                    id=uuid4(),
                    run_id=run_id,
                    task_id=None,
                    attempt_id=None,
                    run_stage_attempt_id=stage_attempt_id,
                    trace_id=f"trace-{idx}",
                    model="test-model",
                    turn=idx + 1,
                    agent_spec_hash="a" * 64,
                    input_tokens=10 + idx,
                    output_tokens=5,
                    cached_input_tokens=0,
                    cost_usd=0.001 * (idx + 1),
                )
                session.add(row)

        hist = await client.get(f"/api/v1/runs/{run_id}/history")
        assert hist.status_code == 200
        body = hist.json()
        assert body["run_id"] == str(run_id)
        assert len(body["samples"]) == 2
        assert body["samples"][0]["model"] == "test-model"
        assert body["samples"][0]["input_tokens"] == 10

        # Missing run 404
        missing = await client.get(f"/api/v1/runs/{uuid4()}/history")
        assert missing.status_code == 404
