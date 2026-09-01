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
        max_concurrency: int | None = None,
        block_ms: int = 1_000,
    ) -> None:
        capacity = batch_size if max_concurrency is None else max_concurrency
        if batch_size < 1 or capacity < 1 or block_ms < 1:
            raise ValueError("worker batch size, concurrency and block time must be positive")
        self._inbox = inbox
        self._executor = executor
        self._batch_size = batch_size
        self._max_concurrency = capacity
        self._block_ms = block_ms

    async def _process_record(self, record: RedisStreamRecord) -> None:
        message = DispatchMessage.model_validate(record.payload)
        await self._executor.execute(message)
        await self._inbox.acknowledge(record)

    async def process_once(self) -> int:
        records = await self._inbox.read(
            count=min(self._batch_size, self._max_concurrency), block_ms=self._block_ms
        )
        executions = [asyncio.create_task(self._process_record(record)) for record in records]
        try:
            results = await asyncio.gather(*executions, return_exceptions=True)
        except BaseException:
            for execution in executions:
                execution.cancel()
            await asyncio.gather(*executions, return_exceptions=True)
            raise
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return len(records)

    @staticmethod
    def _report_completion(execution: asyncio.Task[None]) -> None:
        try:
            execution.result()
        except (Exception, asyncio.CancelledError) as error:
            # Each envelope has its own execution task and lease heartbeat.
            # Its failure/cancellation must not terminate unrelated executions.
            logger.error(
                "worker_task_failed", error_type=type(error).__name__, error_message=str(error)
            )

    async def run(self, stop: asyncio.Event) -> None:
        executions: set[asyncio.Task[None]] = set()
        reading: asyncio.Task[tuple[RedisStreamRecord, ...]] | None = None
        stopping = asyncio.create_task(stop.wait())
        failures = 0
        try:
            while not stop.is_set():
                available = self._max_concurrency - len(executions)
                if reading is None and available:
                    # Never prefetch leased work behind a local execution queue.
                    reading = asyncio.create_task(self._inbox.read(
                        count=min(self._batch_size, available), block_ms=self._block_ms
                    ))
                waiting: list[asyncio.Task[object]] = [*executions, stopping]
                if reading is not None:
                    waiting.append(reading)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for execution in executions.intersection(done):
                    executions.remove(execution)
                    self._report_completion(execution)
                if stop.is_set():
                    break
                if reading is None or reading not in done:
                    continue
                completed_read, reading = reading, None
                try:
                    records = completed_read.result()
                except Exception as error:
                    failures += 1
                    logger.error(
                        "worker_cycle_failed",
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.25 * 2 ** min(failures, 5))
                    except TimeoutError:
                        pass
                    continue
                failures = 0
                executions.update(
                    asyncio.create_task(self._process_record(record)) for record in records
                )
        except BaseException:
            for execution in executions:
                execution.cancel()
            raise
        finally:
            # A stop signal stops intake and drains started work with heartbeats
            # intact. Cancelling the service cancels and joins every child instead.
            auxiliary: list[asyncio.Task[object]] = [stopping]
            if reading is not None:
                auxiliary.append(reading)
            for task in auxiliary:
                task.cancel()
            try:
                await asyncio.gather(*auxiliary, return_exceptions=True)
                await asyncio.gather(*executions, return_exceptions=True)
            except BaseException:
                for task in [*auxiliary, *executions]:
                    task.cancel()
                await asyncio.gather(*auxiliary, *executions, return_exceptions=True)
                raise
            for execution in executions:
                self._report_completion(execution)


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
