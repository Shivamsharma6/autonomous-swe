from __future__ import annotations

import asyncio
import sys
from uuid import UUID

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


async def resolve_parked(project_id: str, task_id: str, resolution: str) -> str:
    if resolution not in {"fail", "retry"}:
        raise SystemExit("resolution must be 'fail' or 'retry'")
    settings = Settings()
    database = Database(settings.database_url)
    reconciler = ReconciliationService(database=database)
    try:
        action = await reconciler.resolve_needs_reconciliation(
            project_id=UUID(project_id),
            task_id=UUID(task_id),
            resolution=resolution,  # type: ignore[arg-type]
        )
        return f"resolved as {action.value}"
    finally:
        await database.dispose()


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 4 and args[0] == "resolve":
        print(asyncio.run(resolve_parked(args[1], args[2], args[3])))
    else:
        print(asyncio.run(reconcile_all()))
