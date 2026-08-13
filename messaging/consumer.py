from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from random import Random, SystemRandom
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from messaging.redis_streams import RedisStreamRecord, RedisStreamsTransport
from persistence.repositories import DomainRepository
from persistence.tables import ConsumerDeliveryRow, DeadLetterRow

DeliveryHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


class DeliveryOutcome(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    DEFERRED = "DEFERRED"
    RETRY = "RETRY"
    DEAD_LETTERED = "DEAD_LETTERED"


@dataclass(frozen=True, slots=True)
class DeliveryRetryPolicy:
    max_attempts: int = 8
    base_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    jitter: bool = True

    def delay_for(self, attempt: int, *, random: Random | None = None) -> timedelta:
        candidate = self.base_delay * (1 << max(0, attempt - 1))
        ceiling = candidate if candidate <= self.max_delay else self.max_delay
        if not self.jitter:
            return ceiling
        generator = random or SystemRandom()
        return timedelta(seconds=generator.uniform(0.0, ceiling.total_seconds()))


class DeliveryConsumer:
    def __init__(
        self,
        database: Any,
        transport: RedisStreamsTransport,
        *,
        stream: str,
        group: str,
        consumer: str,
        retry_policy: DeliveryRetryPolicy | None = None,
    ) -> None:
        self._database = database
        self._transport = transport
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._retry_policy = retry_policy or DeliveryRetryPolicy()

    async def process(
        self,
        record: RedisStreamRecord,
        handler: DeliveryHandler,
        *,
        now: datetime | None = None,
    ) -> DeliveryOutcome:
        timestamp = now or datetime.now(UTC)
        if await self._is_deferred(record.event_id, now=timestamp):
            return DeliveryOutcome.DEFERRED
        duplicate = False
        try:
            async with self._database.transaction() as session:
                claimed = await DomainRepository().claim_consumer_receipt(
                    session,
                    consumer=self._consumer,
                    event_id=record.event_id,
                )
                if not claimed:
                    duplicate = True
                else:
                    await handler(session, record.payload)
                    await session.flush()
        except Exception as error:
            attempts = await self._record_failure(record, error=error, now=timestamp)
            if attempts >= self._retry_policy.max_attempts:
                await self._transport.acknowledge(self._stream, self._group, record.stream_id)
                return DeliveryOutcome.DEAD_LETTERED
            return DeliveryOutcome.RETRY

        await self._transport.acknowledge(self._stream, self._group, record.stream_id)
        return DeliveryOutcome.DUPLICATE if duplicate else DeliveryOutcome.APPLIED

    async def _is_deferred(self, event_id: UUID, *, now: datetime) -> bool:
        async with self._database.transaction() as session:
            delivery = await session.scalar(
                select(ConsumerDeliveryRow).where(
                    ConsumerDeliveryRow.consumer == self._consumer,
                    ConsumerDeliveryRow.event_id == event_id,
                )
            )
            return bool(
                delivery is not None
                and delivery.status == "RETRY"
                and delivery.next_attempt_at > now
            )

    async def _record_failure(
        self, record: RedisStreamRecord, *, error: Exception, now: datetime
    ) -> int:
        async with self._database.transaction() as session:
            delivery = await session.scalar(
                select(ConsumerDeliveryRow)
                .where(
                    ConsumerDeliveryRow.consumer == self._consumer,
                    ConsumerDeliveryRow.event_id == record.event_id,
                )
                .with_for_update()
            )
            if delivery is None:
                delivery = ConsumerDeliveryRow(
                    consumer=self._consumer,
                    event_id=record.event_id,
                    topic=record.topic,
                )
                session.add(delivery)
                await session.flush()
            delivery.attempts += 1
            delivery.last_error = str(error)[:20_000]
            delivery.updated_at = now
            if delivery.attempts >= self._retry_policy.max_attempts:
                delivery.status = "DEAD_LETTERED"
                delivery.next_attempt_at = now
                statement = (
                    insert(DeadLetterRow)
                    .values(
                        event_id=record.event_id,
                        consumer=self._consumer,
                        topic=record.topic,
                        payload=record.payload,
                        attempts=delivery.attempts,
                        last_error=delivery.last_error,
                        causation_chain=[
                            str(value) for value in causation_chain(record.event_id, record.payload)
                        ],
                    )
                    .on_conflict_do_nothing(constraint="uq_dead_letter_consumer_event")
                )
                await session.execute(statement)
            else:
                delivery.status = "RETRY"
                delivery.next_attempt_at = now + self._retry_policy.delay_for(delivery.attempts)
            await session.flush()
            return cast(int, delivery.attempts)


def causation_chain(event_id: UUID, payload: dict[str, Any]) -> tuple[UUID, ...]:
    result: list[UUID] = []
    for value in payload.get("causation_chain", []):
        try:
            parsed = UUID(str(value))
        except (TypeError, ValueError):
            continue
        if parsed not in result:
            result.append(parsed)
    if event_id not in result:
        result.append(event_id)
    return tuple(result)
