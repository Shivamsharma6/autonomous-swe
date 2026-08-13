from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from messaging.consumer import DeliveryConsumer, DeliveryOutcome, DeliveryRetryPolicy
from messaging.redis_streams import RedisStreamRecord, RedisStreamsTransport
from persistence.tables import ConsumerDeliveryRow, ConsumerReceiptRow, DeadLetterRow
from tests.integration.messaging.helpers import event_payload, seed_task


@pytest.mark.asyncio
async def test_concurrent_failures_increment_one_canonical_delivery_record(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    payload = event_payload(ids, event_id)
    transport = RedisStreamsTransport(redis_client)
    record = RedisStreamRecord(
        stream="agent-events",
        stream_id="1-0",
        event_id=event_id,
        topic="agent-events",
        payload=payload,
    )
    consumer = DeliveryConsumer(
        database,
        transport,
        stream="agent-events",
        group="workers",
        consumer="worker-a",
        retry_policy=DeliveryRetryPolicy(jitter=False),
    )

    async def fail(_: Any, __: dict[str, Any]) -> None:
        raise TimeoutError("temporary failure")

    outcomes = await asyncio.gather(
        consumer.process(record, fail),
        consumer.process(record, fail),
    )

    assert outcomes == [DeliveryOutcome.RETRY, DeliveryOutcome.RETRY]
    async with database.transaction() as session:
        delivery = await session.scalar(select(ConsumerDeliveryRow))
    assert delivery is not None
    assert delivery.attempts == 2


@pytest.mark.asyncio
async def test_eighth_failure_creates_canonical_dead_letter_and_acknowledges_transport(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    payload = event_payload(ids, event_id)
    transport = RedisStreamsTransport(redis_client)
    await transport.ensure_group("agent-events", "workers")
    await transport.publish("agent-events", event_id, payload)
    pending = (await transport.read("agent-events", "workers", "worker-a", count=1, block_ms=1))[0]
    record = RedisStreamRecord(
        stream=pending.stream,
        stream_id=pending.stream_id,
        event_id=pending.event_id,
        topic=pending.topic,
        payload=pending.payload,
    )
    policy = DeliveryRetryPolicy(
        max_attempts=8,
        base_delay=timedelta(milliseconds=1),
        max_delay=timedelta(milliseconds=10),
        jitter=False,
    )
    consumer = DeliveryConsumer(
        database,
        transport,
        stream="agent-events",
        group="workers",
        consumer="worker-a",
        retry_policy=policy,
    )

    async def fail(_: Any, __: dict[str, Any]) -> None:
        raise TimeoutError("UAMS dependency unavailable")

    start = datetime.now(UTC)
    outcomes = [
        await consumer.process(record, fail, now=start + timedelta(seconds=attempt))
        for attempt in range(8)
    ]

    assert outcomes[:7] == [DeliveryOutcome.RETRY] * 7
    assert outcomes[7] is DeliveryOutcome.DEAD_LETTERED
    assert (
        await redis_client.xpending_range("agent-events", "workers", min="-", max="+", count=10)
        == []
    )
    async with database.transaction() as session:
        dead_letter = await session.scalar(select(DeadLetterRow))
        delivery = await session.scalar(select(ConsumerDeliveryRow))
        receipts = await session.scalar(select(func.count()).select_from(ConsumerReceiptRow))
    assert dead_letter is not None
    assert dead_letter.consumer == "worker-a"
    assert dead_letter.event_id == event_id
    assert dead_letter.attempts == 8
    assert dead_letter.last_error == "UAMS dependency unavailable"
    assert dead_letter.causation_chain[-1] == str(event_id)
    assert delivery is not None
    assert delivery.status == "DEAD_LETTERED"
    assert receipts == 0
