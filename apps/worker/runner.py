from __future__ import annotations

import asyncio
import signal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from apps.dispatcher.main import DispatchMessage
from messaging.redis_streams import RedisStreamRecord, RedisStreamsTransport
from observability.logging import get_structured_logger

logger = get_structured_logger("autoswe.worker")


class WorkerOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    FAILED = "FAILED"


class DispatchedWorkflowExecutor(Protocol):
    async def execute(self, message: DispatchMessage) -> WorkerOutcome: ...


class DispatchInbox(Protocol):
    async def read(self, *, count: int, block_ms: int) -> tuple[RedisStreamRecord, ...]: ...

    async def acknowledge(self, record: RedisStreamRecord) -> bool: ...


class RedisDispatchInbox:
    def __init__(
        self,
        transport: RedisStreamsTransport,
        *,
        group: str,
        consumer: str,
    ) -> None:
        self._transport = transport
        self._group = group
        self._consumer = consumer
        self._ready = False

    async def setup(self) -> None:
        await self._transport.ensure_group("task-dispatch", self._group)
        self._ready = True

    async def read(self, *, count: int, block_ms: int) -> tuple[RedisStreamRecord, ...]:
        try:
            if not self._ready:
                await self.setup()
            return await self._transport.read(
                "task-dispatch",
                self._group,
                self._consumer,
                count=count,
                block_ms=block_ms,
            )
        except Exception:
            self._ready = False
            raise

    async def acknowledge(self, record: RedisStreamRecord) -> bool:
        return await self._transport.acknowledge(
            "task-dispatch",
            self._group,
            record.stream_id,
        )


class WorkerService:
    """Execute only persisted dispatcher envelopes; no task discovery exists here."""

    def __init__(
        self,
        *,
        inbox: DispatchInbox,
        executor: DispatchedWorkflowExecutor,
        batch_size: int = 1,
        block_ms: int = 1_000,
    ) -> None:
        if batch_size < 1 or block_ms < 1:
            raise ValueError("worker batch size and block time must be positive")
        self._inbox = inbox
        self._executor = executor
        self._batch_size = batch_size
        self._block_ms = block_ms

    async def process_once(self) -> int:
        records = await self._inbox.read(count=self._batch_size, block_ms=self._block_ms)
        completed = 0
        for record in records:
            message = DispatchMessage.model_validate(record.payload)
            await self._executor.execute(message)
            await self._inbox.acknowledge(record)
            completed += 1
        return completed

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                await self.process_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Leave the envelope unacknowledged. Lease reconciliation owns
                # recovery; a single failed task must not kill other workers.
                failures += 1
                logger.error(
                    "worker_cycle_failed", error_type=type(error).__name__, error_message=str(error)
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25 * 2 ** min(failures, 5))
                except TimeoutError:
                    pass


def install_worker_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: stop.set())


def dispatch_thread_id(message: DispatchMessage) -> str:
    return f"task:{message.task_id}:attempt:{message.attempt_id}"


def require_matching_worker_identity(message: DispatchMessage, task_id: UUID) -> None:
    if message.task_id != task_id:
        raise PermissionError("worker dispatch identity does not match the checkpoint thread")
