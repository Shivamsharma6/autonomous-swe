from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from domain.enums import ArtifactState, RiskLevel, TaskType
from domain.models import BudgetPolicy, TaskSpec
from persistence.artifacts import ArtifactIntegrityError, ArtifactService, ArtifactStore
from persistence.repositories import DomainRepository
from persistence.tables import AuditEventRow, OutboxRow


@pytest.mark.asyncio
async def test_corrupt_artifact_is_detected_quarantined_and_excluded(
    database: object, tmp_path: Path
) -> None:
    repository = DomainRepository()
    store = ArtifactStore(tmp_path / "artifacts")
    service = ArtifactService(store=store, repository=repository)
    project_id = uuid4()
    repository_id = uuid4()
    run_id = uuid4()
    task = TaskSpec(
        id=uuid4(),
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Validate evidence",
        description="Create and verify the test evidence artifact.",
        task_type=TaskType.VALIDATION,
        assigned_capability="validator",
        acceptance_criteria=("Evidence hash matches",),
        allowed_tools=("read_file",),
        risk_ceiling=RiskLevel.LOW,
        budget=BudgetPolicy(cost_usd=0.1, wall_time_seconds=30),
    )

    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Integrity project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/integrity.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Verify artifacts",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        await repository.create_task(session, run_id=run_id, task=task)
        stored = await service.put(
            session,
            content=b'{"passed": true}',
            media_type="application/json",
            project_id=project_id,
            run_id=run_id,
            task_id=task.id,
        )

    object_path = store.root / stored.storage_key
    object_path.chmod(0o600)
    object_path.write_bytes(b'{"passed": false, "tampered": true}')

    async with database.transaction() as session:
        with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
            await service.get_verified(
                session,
                project_id=project_id,
                artifact_id=stored.artifact_id,
            )

    async with database.transaction() as session:
        row = await repository.get_artifact(
            session,
            project_id=project_id,
            artifact_id=stored.artifact_id,
        )
        valid_evidence = await repository.list_valid_artifacts(
            session,
            project_id=project_id,
            task_id=task.id,
        )
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(AuditEventRow.event_type == "artifact.corrupt")
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxRow)
            .where(OutboxRow.topic == "artifact-integrity")
        )

    assert row is not None
    assert row.state == ArtifactState.CORRUPT
    assert valid_evidence == ()
    assert audit_count == 1
    assert outbox_count == 1
    assert not object_path.exists()
    assert any(path.is_file() for path in (store.root / "quarantine").rglob("*"))


@pytest.mark.asyncio
async def test_valid_artifact_metadata_matches_stored_object(
    database: object, tmp_path: Path
) -> None:
    repository = DomainRepository()
    store = ArtifactStore(tmp_path / "artifacts")
    service = ArtifactService(store=store, repository=repository)
    project_id, repository_id, run_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    task = TaskSpec(
        id=task_id,
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Store evidence",
        description="Store verified evidence.",
        task_type=TaskType.VALIDATION,
        assigned_capability="validator",
        acceptance_criteria=("Evidence is stored",),
    )
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Evidence project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/evidence.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Store evidence",
            baseline_commit="a" * 40,
        )
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        await repository.create_task(session, run_id=run_id, task=task)
        stored = await service.put(
            session,
            content=b"verified report",
            media_type="text/plain",
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
        )

    async with database.transaction() as session:
        content = await service.get_verified(
            session,
            project_id=project_id,
            artifact_id=stored.artifact_id,
        )
        metadata = await repository.get_artifact(
            session,
            project_id=project_id,
            artifact_id=stored.artifact_id,
        )

    assert content == b"verified report"
    assert metadata is not None
    assert metadata.sha256 == stored.sha256
    assert store.verify(stored).actual_sha256 == metadata.sha256
