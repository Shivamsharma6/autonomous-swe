"""Integration-sink rule and verification-evidence collection for release gates."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select

from domain.models import TaskPlan
from persistence.artifacts import ArtifactService
from persistence.database import Database
from persistence.tables import ArtifactRow, TaskRow


def integration_sink(tasks: tuple[TaskRow, ...]) -> TaskRow:
    """The single final VALIDATION task that no other task depends on."""
    depended_on = {UUID(value) for task in tasks for value in task.dependencies}
    sinks = tuple(task for task in tasks if task.id not in depended_on)
    validations = tuple(task for task in sinks if task.task_type.value == "VALIDATION")
    if len(validations) != 1:
        raise RuntimeError("latest plan revision must have one final validation sink")
    return validations[0]


def require_integration_plan(plan: TaskPlan) -> None:
    """Enforce one transitive VALIDATION sink over the durable plan contract."""
    tasks = {task.id: task for task in plan.tasks}
    depended_on = {
        dependency for task in plan.tasks for dependency in task.dependencies
    }
    sinks = tuple(task for task in plan.tasks if task.id not in depended_on)

    def ancestors(task_id: UUID) -> set[UUID]:
        found: set[UUID] = set()
        pending = list(tasks[task_id].dependencies)
        while pending:
            dependency = pending.pop()
            if dependency in found:
                continue
            found.add(dependency)
            pending.extend(tasks[dependency].dependencies)
        return found

    valid = tuple(
        task
        for task in sinks
        if task.task_type.value == "VALIDATION"
        and ancestors(task.id) | {task.id} == set(tasks)
    )
    if len(valid) != 1:
        raise RuntimeError(
            "current plan requires exactly one final VALIDATION sink covering every task"
        )


async def verification_evidence(
    database: Database,
    artifacts: ArtifactService,
    tasks: tuple[TaskRow, ...],
) -> tuple[tuple[str, ...], tuple[UUID, ...]]:
    """Collect rehashed artifact evidence and explicit verification failures."""
    failures: list[str] = []
    evidence: list[UUID] = []
    verified_tasks: set[UUID] = set()
    if not tasks:
        return ("latest plan revision has no tasks",), ()
    project_id = tasks[0].project_id
    async with database.transaction() as session:
        rows = tuple(
            (
                await session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.task_id.in_([task.id for task in tasks]))
                    .order_by(ArtifactRow.created_at, ArtifactRow.id)
                )
            ).all()
        )
        for row in rows:
            try:
                content = await artifacts.get_verified(
                    session,
                    project_id=project_id,
                    artifact_id=row.id,
                )
            except Exception as error:
                failures.append(
                    f"artifact {row.id} failed integrity verification: "
                    f"{type(error).__name__}"
                )
                continue
            evidence.append(row.id)
            try:
                document = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            output = document.get("output") if isinstance(document, dict) else None
            if isinstance(output, dict) and output.get("verification_passed") is False:
                failures.append(str(output.get("summary") or "verification failed"))
            if isinstance(output, dict) and output.get("verification_passed") is True:
                verified_tasks.add(row.task_id)
    if not evidence:
        failures.append("latest plan revision produced no verified evidence")
    sink = integration_sink(tasks)
    if sink.id not in verified_tasks:
        failures.append("final validation sink produced no passing verification evidence")
    return tuple(failures), tuple(evidence)
