from __future__ import annotations

import asyncio

from sqlalchemy import select

from execution.scheduler.reconciliation import ReconciliationService
from infrastructure.config import Settings
from persistence.database import Database
from persistence.tables import TaskRow


async def reconcile_all() -> dict[str, int]:
    settings = Settings()
    database = Database(settings.database_url)
    reconciler = ReconciliationService(database=database)
    counts: dict[str, int] = {}
    try:
        async with database.sessions() as session:
            identities = tuple(
                (
                    await session.execute(
                        select(TaskRow.project_id, TaskRow.id).order_by(TaskRow.created_at)
                    )
                ).all()
            )
        for project_id, task_id in identities:
            action = await reconciler.reconcile(project_id=project_id, task_id=task_id)
            counts[action.value] = counts.get(action.value, 0) + 1
        return counts
    finally:
        await database.dispose()


if __name__ == "__main__":
    print(asyncio.run(reconcile_all()))
