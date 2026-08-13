from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from persistence.database import Database
from persistence.tables import AuditEventRow


@dataclass(frozen=True, slots=True)
class EventCursor:
    created_at: datetime
    event_id: UUID


class PostgresTaskEventSource:
    """Read canonical task-scoped events; the API starts no transport consumer loop."""

    def __init__(self, database: Database, *, poll_seconds: float = 0.5) -> None:
        self._database = database
        self._poll_seconds = poll_seconds

    async def next_events(
        self,
        task_id: UUID,
        *,
        after: EventCursor | None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        async with self._database.sessions() as session:
            statement = select(AuditEventRow).where(
                or_(
                    AuditEventRow.aggregate_id == task_id,
                    AuditEventRow.payload["task_id"].astext == str(task_id),
                )
            )
            if after is not None:
                statement = statement.where(
                    or_(
                        AuditEventRow.created_at > after.created_at,
                        (
                            (AuditEventRow.created_at == after.created_at)
                            & (AuditEventRow.id > after.event_id)
                        ),
                    )
                )
            rows = tuple(
                (
                    await session.scalars(
                        statement.order_by(AuditEventRow.created_at, AuditEventRow.id).limit(limit)
                    )
                ).all()
            )
        if not rows:
            await asyncio.sleep(self._poll_seconds)
            return ()
        return tuple(
            {
                "event_id": str(row.id),
                "event_type": row.event_type,
                "task_id": str(task_id),
                "payload": row.payload,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        )
