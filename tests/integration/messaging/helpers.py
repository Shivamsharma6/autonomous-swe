from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from domain.enums import RiskLevel, TaskType
from domain.models import BudgetPolicy, ResourceEstimate, TaskSpec
from persistence.repositories import DomainRepository


async def seed_task(database: Any, *, run_state: str = "RUNNING") -> dict[str, UUID]:
    repository = DomainRepository()
    project_id, repository_id, run_id, task_id, attempt_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="Messaging project")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/messaging.git",
            default_branch="main",
        )
        run = await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Deliver durable messages",
            baseline_commit="b" * 40,
        )
        run.state = run_state
        await repository.create_plan_revision(session, run_id=run_id, revision=1, plan={})
        task = TaskSpec(
            id=task_id,
            plan_revision=1,
            project_id=project_id,
            repository_id=repository_id,
            title="deliver-message",
            description="Exercise at-least-once delivery",
            task_type=TaskType.IMPLEMENTATION,
            assigned_capability="coder",
            acceptance_criteria=("Message effect occurs once",),
            risk_ceiling=RiskLevel.LOW,
            budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
            estimate=ResourceEstimate(model_tokens=100, wall_time_seconds=60),
        )
        await repository.create_task(session, run_id=run_id, task=task)
        await repository.create_attempt(
            session,
            attempt_id=attempt_id,
            task_id=task_id,
            agent_spec_hash="c" * 64,
        )
    return {
        "project_id": project_id,
        "repository_id": repository_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
    }


def event_payload(ids: dict[str, UUID], event_id: UUID) -> dict[str, Any]:
    return {
        "event_id": str(event_id),
        "run_id": str(ids["run_id"]),
        "task_id": str(ids["task_id"]),
        "causation_id": str(uuid4()),
        "correlation_id": str(ids["run_id"]),
        "causation_chain": [str(uuid4()), str(uuid4())],
        "created_at": datetime.now(UTC).isoformat(),
    }
