from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from messaging.consumer import DeliveryConsumer, DeliveryOutcome
from messaging.redis_streams import RedisStreamsTransport
from messaging.retention import RetentionPolicy, RetentionService
from observability.logging import get_structured_logger
from persistence.database import Database

logger = get_structured_logger("autoswe.dispatcher.background")

CONSUMER_GROUP = "autoswe"
EVENT_STREAMS: tuple[str, ...] = (
    "task-state",
    "workflow-state",
    "artifact-integrity",
    "reconciliation",
)


class EventConsumptionLoop:
    """Consume published outbox topics so receipts, retry/dead-letter handling,
    PEL recovery, and stream trimming stay operational instead of test-only."""

    def __init__(
        self,
        *,
        database: Database,
        transport: RedisStreamsTransport,
        consumer_name: str,
        streams: tuple[str, ...] = EVENT_STREAMS,
        reclaim_after: timedelta = timedelta(minutes=5),
        batch_size: int = 32,
        block_ms: int = 1_000,
        poll_seconds: float = 0.25,
    ) -> None:
        if not consumer_name.strip():
            raise ValueError("event consumer name must be valid")
        self._database = database
        self._transport = transport
        self._streams = streams
        self._consumer_name = consumer_name
        self._reclaim_after = reclaim_after
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._poll_seconds = poll_seconds
        self._consumers = {
            stream: DeliveryConsumer(
                database,
                transport,
                stream=stream,
                group=CONSUMER_GROUP,
                consumer=consumer_name,
            )
            for stream in streams
        }

    async def _apply_event(self, session: Any, payload: dict[str, Any]) -> None:
        # Canonical domain truth already lives in PostgreSQL; the durable
        # consumer receipt written by DeliveryConsumer is the required effect.
        _ = payload
        _ = session

    async def cycle(self) -> int:
        processed = 0
        for stream, consumer in self._consumers.items():
            await self._transport.ensure_group(stream, CONSUMER_GROUP)
            stale = await self._transport.reclaim(
                stream,
                CONSUMER_GROUP,
                self._consumer_name,
                min_idle=self._reclaim_after,
                count=self._batch_size,
            )
            fresh = await self._transport.read(
                stream,
                CONSUMER_GROUP,
                self._consumer_name,
                count=self._batch_size,
                block_ms=self._block_ms,
            )
            for record in (*stale, *fresh):
                outcome = await consumer.process(record, self._apply_event)
                if outcome is not DeliveryOutcome.DEFERRED:
                    processed += 1
        return processed

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                processed = await self.cycle()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                processed = 0
                logger.error(
                    "event_consumption_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            if processed:
                continue
            delay = self._poll_seconds * (2**min(failures, 4))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass


class RetentionLoop:
    """Enforce the documented retention policy on a fixed cadence."""

    def __init__(
        self,
        *,
        database: Database,
        transport: RedisStreamsTransport,
        policy: RetentionPolicy | None = None,
        streams: tuple[str, ...] = EVENT_STREAMS,
        interval: timedelta = timedelta(hours=1),
    ) -> None:
        self._service = RetentionService(
            database,
            transport,
            policy=policy or RetentionPolicy(),
        )
        self._streams = streams
        self._interval = interval

    async def enforce_once(self) -> None:
        result = await self._service.enforce(streams=self._streams)
        if (
            result.redis_entries_deleted
            or result.message_payloads_purged
            or result.dead_letters_deleted
            or result.operational_rows_deleted
        ):
            logger.info(
                "retention_enforced",
                redis_entries_deleted=result.redis_entries_deleted,
                message_payloads_purged=result.message_payloads_purged,
                dead_letters_deleted=result.dead_letters_deleted,
                operational_rows_deleted=result.operational_rows_deleted,
                enforced_at=datetime.now(UTC).isoformat(),
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.enforce_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "retention_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval.total_seconds())
            except TimeoutError:
                pass
