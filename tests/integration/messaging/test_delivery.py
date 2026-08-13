from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from domain.messages import ContextHandoff
from messaging.consumer import DeliveryConsumer, DeliveryOutcome
from messaging.outbox import OutboxPublisher, TransactionalMessageBus
from messaging.redis_streams import RedisStreamRecord, RedisStreamsTransport
from persistence.repositories import DomainRepository
from persistence.tables import AgentMessageRow, AuditEventRow, ConsumerReceiptRow, OutboxRow
from tests.integration.messaging.helpers import event_payload, seed_task


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterPublishTransport:
    def __init__(self, inner: RedisStreamsTransport) -> None:
        self.inner = inner
        self.crashed = False

    async def publish(self, topic: str, event_id: UUID, payload: dict[str, Any]) -> str:
        stream_id = await self.inner.publish(topic, event_id, payload)
        if not self.crashed:
            self.crashed = True
            raise SimulatedProcessCrash("process died after Redis accepted the event")
        return stream_id


class FailingOutboxRepository(DomainRepository):
    async def enqueue_event(self, *args: Any, **kwargs: Any) -> OutboxRow:
        raise RuntimeError("outbox insert unavailable")


@pytest.mark.asyncio
async def test_typed_message_and_outbox_commit_or_rollback_together(database: Any) -> None:
    ids = await seed_task(database)
    now = datetime.now(UTC)
    message = ContextHandoff(
        sender="researcher",
        recipient="coder",
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        created_at=now,
        causation_id=uuid4(),
        correlation_id=ids["run_id"],
        summary="The API boundary and persistence contract are relevant.",
    )
    bus = TransactionalMessageBus(database)

    assert await bus.send(message) == message.message_id
    async with database.transaction() as session:
        assert await session.get(AgentMessageRow, message.message_id) is not None
        assert await session.get(OutboxRow, message.message_id) is not None

    failed = ContextHandoff.model_validate(
        message.model_dump(exclude={"content_hash"}) | {"message_id": uuid4()}
    )
    with pytest.raises(RuntimeError, match="outbox insert unavailable"):
        await TransactionalMessageBus(database, FailingOutboxRepository()).send(failed)
    async with database.transaction() as session:
        assert await session.get(AgentMessageRow, failed.message_id) is None
        assert await session.get(OutboxRow, failed.message_id) is None


@pytest.mark.asyncio
async def test_concurrent_publishers_claim_each_outbox_row_once(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    events = [uuid4(), uuid4()]
    async with database.transaction() as session:
        for event_id in events:
            await DomainRepository().enqueue_event(
                session,
                event_id=event_id,
                topic="agent-events",
                payload=event_payload(ids, event_id),
            )
    transport = RedisStreamsTransport(redis_client)
    first = OutboxPublisher(database, transport, publisher_id="publisher-a")
    second = OutboxPublisher(database, transport, publisher_id="publisher-b")

    counts = await asyncio.gather(first.publish_batch(limit=2), second.publish_batch(limit=2))

    assert sum(counts) == 2
    assert await redis_client.xlen("agent-events") == 2
    async with database.transaction() as session:
        rows = tuple((await session.scalars(select(OutboxRow))).all())
    assert {row.attempts for row in rows} == {1}
    assert all(row.published_at is not None for row in rows)


@pytest.mark.asyncio
async def test_crash_after_publish_republishes_same_stable_event(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    async with database.transaction() as session:
        await DomainRepository().enqueue_event(
            session,
            event_id=event_id,
            topic="agent-events",
            payload=event_payload(ids, event_id),
        )
    now = datetime.now(UTC)
    transport = CrashAfterPublishTransport(RedisStreamsTransport(redis_client))
    publisher = OutboxPublisher(
        database,
        transport,
        publisher_id="crashy",
        retry_base=timedelta(seconds=1),
    )

    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish_batch(limit=1, now=now)
    assert await publisher.publish_batch(limit=1, now=now + timedelta(seconds=1)) == 0
    async with database.transaction() as session:
        crashed_row = await session.get(OutboxRow, event_id)
    assert crashed_row is not None
    assert crashed_row.attempts == 1
    assert crashed_row.published_at is None
    assert crashed_row.claimed_until == now + timedelta(seconds=30)

    assert await publisher.publish_batch(limit=1, now=now + timedelta(seconds=31)) == 1

    entries = await redis_client.xrange("agent-events")
    assert len(entries) == 2
    assert {fields["event_id"] for _, fields in entries} == {str(event_id)}
    async with database.transaction() as session:
        row = await session.get(OutboxRow, event_id)
    assert row is not None
    assert row.attempts == 2
    assert row.published_at is not None


@pytest.mark.asyncio
async def test_redis_loss_is_recovered_from_canonical_outbox(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    payload = event_payload(ids, event_id)
    async with database.transaction() as session:
        await DomainRepository().enqueue_event(
            session,
            event_id=event_id,
            topic="agent-events",
            payload=payload,
        )
    publisher = OutboxPublisher(
        database,
        RedisStreamsTransport(redis_client),
        publisher_id="reconciler",
    )
    assert await publisher.publish_batch(limit=1) == 1
    await redis_client.flushdb()

    assert await publisher.requeue(event_id)
    assert await publisher.publish_batch(limit=1) == 1

    entries = await redis_client.xrange("agent-events")
    assert len(entries) == 1
    assert entries[0][1]["event_id"] == str(event_id)
    assert entries[0][1]["payload"]


@pytest.mark.asyncio
async def test_pending_entry_is_reclaimed_and_duplicate_effect_is_skipped(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    payload = event_payload(ids, event_id)
    transport = RedisStreamsTransport(redis_client)
    await transport.ensure_group("agent-events", "workers")
    await transport.publish("agent-events", event_id, payload)
    first_read = await transport.read("agent-events", "workers", "worker-a", count=1, block_ms=1)
    assert len(first_read) == 1
    reclaimed = await transport.reclaim(
        "agent-events", "workers", "worker-b", min_idle=timedelta(0), count=1
    )
    assert len(reclaimed) == 1
    effects: list[UUID] = []

    async def apply_effect(session: Any, body: dict[str, Any]) -> None:
        effect_id = UUID(str(body["event_id"]))
        effects.append(effect_id)
        await DomainRepository().append_audit(
            session,
            event_id=effect_id,
            event_type="test.effect",
            aggregate_type="task",
            aggregate_id=ids["task_id"],
            payload={"event_id": str(effect_id)},
            correlation_id=ids["run_id"],
            causation_id=effect_id,
        )

    consumer = DeliveryConsumer(
        database,
        transport,
        stream="agent-events",
        group="workers",
        consumer="worker-b",
    )
    assert await consumer.process(reclaimed[0], apply_effect) is DeliveryOutcome.APPLIED

    duplicate_id = await transport.publish("agent-events", event_id, payload)
    duplicate = RedisStreamRecord(
        stream="agent-events",
        stream_id=duplicate_id,
        event_id=event_id,
        topic="agent-events",
        payload=payload,
    )
    assert await consumer.process(duplicate, apply_effect) is DeliveryOutcome.DUPLICATE

    assert effects == [event_id]
    async with database.transaction() as session:
        receipts = await session.scalar(select(func.count()).select_from(ConsumerReceiptRow))
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditEventRow)
            .where(AuditEventRow.event_type == "test.effect")
        )
    assert receipts == 1
    assert audits == 1
