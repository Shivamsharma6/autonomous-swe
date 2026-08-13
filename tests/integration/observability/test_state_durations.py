from __future__ import annotations

from sqlalchemy import select

from domain.enums import TaskStatus
from persistence.repositories import DomainRepository
from persistence.tables import StateDurationRow
from tests.integration.persistence.test_repositories import create_core


async def test_task_transition_atomically_records_time_in_prior_state(database) -> None:  # type: ignore[no-untyped-def]
    repository = DomainRepository()
    async with database.transaction() as session:
        core = await create_core(repository, session)
        task_id = core["task"].id
        task = await repository.transition_task(
            session,
            project_id=core["project_id"],
            task_id=task_id,
            expected_version=1,
            target=TaskStatus.READY,
        )
        durations = tuple(
            (
                await session.scalars(
                    select(StateDurationRow).where(
                        StateDurationRow.aggregate_type == "task",
                        StateDurationRow.aggregate_id == task_id,
                    )
                )
            ).all()
        )

    assert task.state is TaskStatus.READY
    assert len(durations) == 1
    assert durations[0].state == TaskStatus.PENDING.value
    assert durations[0].duration_seconds >= 0
