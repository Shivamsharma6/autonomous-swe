from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from domain.messages import MessageEnvelope
from persistence.repositories import DomainRepository
from persistence.tables import DeadLetterRow, OutboxRow


class EventTransport(Protocol):
    async def publish(self, topic: str, event_id: UUID, payload: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: UUID
    topic: str
    payload: dict[str, Any]
    attempt: int
    claim_token: UUID


class TransactionalMessageBus:
    """Atomically records a typed message and its stable outbox event."""

    def __init__(self, database: Any, repository: DomainRepository | None = None) -> None:
        self._database = database
        self._repository = repository or DomainRepository()

    async def send(self, message: MessageEnvelope, *, topic: str = "agent-messages") -> UUID:
        async with self._database.transaction() as session:
            await self._repository.persist_message(session, message)
            await self._repository.enqueue_event(
                session,
                event_id=message.message_id,
                topic=topic,
                payload=message.model_dump(mode="json"),
            )
        return message.message_id


class OutboxPublisher:
    def __init__(
        self,
        database: Any,
        transport: EventTransport,
        *,
        publisher_id: str,
        claim_ttl: timedelta = timedelta(seconds=30),
        retry_base: timedelta = timedelta(seconds=1),
        retry_max: timedelta = timedelta(minutes=5),
        max_attempts: int = 8,
    ) -> None:
        self._database = database
        self._transport = transport
        self._publisher_id = publisher_id
        self._claim_ttl = claim_ttl
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._max_attempts = max_attempts

    async def publish_batch(self, *, limit: int = 100, now: datetime | None = None) -> int:
        timestamp = now or datetime.now(UTC)
        claimed = await self._claim(limit=limit, now=timestamp)
        published = 0
        for event in claimed:
            try:
                await self._transport.publish(event.topic, event.event_id, event.payload)
            except Exception as error:
                await self._mark_failed(event, error=error, now=timestamp)
            else:
                if await self._mark_published(event, now=timestamp):
                    published += 1
        return published

    async def requeue(self, event_id: UUID, *, now: datetime | None = None) -> bool:
        """Make a canonical event eligible after disposable transport loss."""
        async with self._database.transaction() as session:
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.event_id == event_id)
                .values(
                    published_at=None,
                    next_attempt_at=now or datetime.now(UTC),
                    publisher=None,
                    claim_token=None,
                    claimed_until=None,
                )
            )
            return bool(result.rowcount)

    async def _claim(self, *, limit: int, now: datetime) -> tuple[ClaimedEvent, ...]:
        if limit < 1:
            return ()
        async with self._database.transaction() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(OutboxRow)
                        .where(
                            OutboxRow.published_at.is_(None),
                            OutboxRow.dead_lettered_at.is_(None),
                            OutboxRow.next_attempt_at <= now,
                            or_(OutboxRow.claimed_until.is_(None), OutboxRow.claimed_until <= now),
                        )
                        .order_by(OutboxRow.created_at, OutboxRow.event_id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            events: list[ClaimedEvent] = []
            for row in rows:
                token = uuid4()
                row.attempts += 1
                row.publisher = self._publisher_id
                row.claim_token = token
                row.claimed_until = now + self._claim_ttl
                events.append(
                    ClaimedEvent(
                        event_id=row.event_id,
                        topic=row.topic,
                        payload=row.payload,
                        attempt=row.attempts,
                        claim_token=token,
                    )
                )
            await session.flush()
            return tuple(events)

    async def _mark_published(self, event: ClaimedEvent, *, now: datetime) -> bool:
        async with self._database.transaction() as session:
            result = await session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.event_id == event.event_id,
                    OutboxRow.claim_token == event.claim_token,
                    OutboxRow.published_at.is_(None),
                )
                .values(
                    published_at=now,
                    last_error=None,
                    publisher=None,
                    claim_token=None,
                    claimed_until=None,
                )
            )
            return bool(result.rowcount)

    async def _mark_failed(self, event: ClaimedEvent, *, error: Exception, now: datetime) -> bool:
        if event.attempt >= self._max_attempts:
            await self._dead_letter(event, error=error, now=now)
            return False
        multiplier = 2 ** max(0, event.attempt - 1)
        delay = min(self._retry_base * multiplier, self._retry_max)
        async with self._database.transaction() as session:
            result = await session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.event_id == event.event_id,
                    OutboxRow.claim_token == event.claim_token,
                )
                .values(
                    next_attempt_at=now + delay,
                    last_error=str(error)[:20_000],
                    publisher=None,
                    claim_token=None,
                    claimed_until=None,
                )
            )
            return bool(result.rowcount)

    async def _dead_letter(
        self, event: ClaimedEvent, *, error: Exception, now: datetime
    ) -> None:
        """Park a poison event: stop retrying, retain the canonical row, and
        record an operator-visible dead letter for inspection/replay."""
        async with self._database.transaction() as session:
            await session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.event_id == event.event_id,
                    OutboxRow.claim_token == event.claim_token,
                )
                .values(
                    dead_lettered_at=now,
                    last_error=str(error)[:20_000],
                    publisher=None,
                    claim_token=None,
                    claimed_until=None,
                )
            )
            statement = (
                pg_insert(DeadLetterRow)
                .values(
                    event_id=event.event_id,
                    consumer=f"outbox:{self._publisher_id}",
                    topic=event.topic,
                    payload=event.payload,
                    attempts=event.attempt,
                    last_error=str(error)[:20_000],
                    causation_chain=[str(event.event_id)],
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_dead_letter_consumer_event")
            )
            await session.execute(statement)
