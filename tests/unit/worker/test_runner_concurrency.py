from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from apps.dispatcher.main import DispatchMessage
from apps.worker.runner import WorkerOutcome, WorkerService
from messaging.redis_streams import RedisStreamRecord


def dispatch_record() -> RedisStreamRecord:
    message = DispatchMessage(
        task_id=uuid4(), project_id=uuid4(), owner="dispatcher-test",
        lease_token=uuid4(), attempt_id=uuid4(),
        expires_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    )
    return RedisStreamRecord(
        stream="task-dispatch", stream_id=str(message.task_id),
        event_id=message.lease_token, topic="task-dispatch",
        payload=message.model_dump(mode="json"),
    )


class QueueInbox:
    def __init__(self, records: tuple[RedisStreamRecord, ...] = ()) -> None:
        self.records: asyncio.Queue[RedisStreamRecord] = asyncio.Queue()
        for record in records:
            self.records.put_nowait(record)
        self.acknowledged: list[RedisStreamRecord] = []
        self.read_counts: list[int] = []
        self.read_started = asyncio.Event()
        self.readers = 0

    async def read(self, *, count: int, block_ms: int) -> tuple[RedisStreamRecord, ...]:
        self.read_counts.append(count)
        self.readers += 1
        self.read_started.set()
        try:
            records = [await self.records.get()]
            while len(records) < count and not self.records.empty():
                records.append(self.records.get_nowait())
            return tuple(records)
        finally:
            self.readers -= 1

    async def acknowledge(self, record: RedisStreamRecord) -> bool:
        self.acknowledged.append(record)
        return True


class GatedExecutor:
    def __init__(self, records: tuple[RedisStreamRecord, ...]) -> None:
        self.started = {record.payload["task_id"]: asyncio.Event() for record in records}
        self.release = {key: asyncio.Event() for key in self.started}
        self.cleaned = {key: asyncio.Event() for key in self.started}
        self.active = 0
        self.peak = 0
        self.failures: dict[str, BaseException] = {}

    async def execute(self, message: DispatchMessage) -> WorkerOutcome:
        key = str(message.task_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started[key].set()
        try:
            await self.release[key].wait()
            if key in self.failures:
                raise self.failures[key]
            return WorkerOutcome.COMPLETED
        finally:
            # A real executor must finish its asynchronous heartbeat/lease cleanup.
            await asyncio.sleep(0)
            self.active -= 1
            self.cleaned[key].set()

    def release_all(self) -> None:
        for release in self.release.values():
            release.set()


async def test_batch_starts_all_leased_tasks_before_waiting_for_the_first() -> None:
    records = tuple(dispatch_record() for _ in range(3))
    inbox = QueueInbox(records)
    executor = GatedExecutor(records)
    stop = asyncio.Event()
    worker = WorkerService(inbox=inbox, executor=executor, batch_size=3, block_ms=1)
    running = asyncio.create_task(worker.run(stop))
    try:
        # Every task must begin (and start its executor heartbeat) while the first
        # is still blocked. Serial execution previously let queued leases expire.
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in executor.started.values())), timeout=1
        )
        assert executor.peak == 3
        stop.set()
        assert not running.done()
        executor.release_all()
        await asyncio.wait_for(running, timeout=1)
        assert len(inbox.acknowledged) == 3
        assert executor.active == 0
    finally:
        executor.release_all()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_worker_refills_a_free_slot_without_waiting_for_slow_siblings() -> None:
    records = tuple(dispatch_record() for _ in range(3))
    keys = [record.payload["task_id"] for record in records]
    inbox = QueueInbox(records)
    executor = GatedExecutor(records)
    stop = asyncio.Event()
    worker = WorkerService(
        inbox=inbox, executor=executor, batch_size=3, max_concurrency=2, block_ms=1
    )
    running = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.wait_for(executor.started[keys[1]].wait(), timeout=1)
        assert not executor.started[keys[2]].is_set()
        executor.release[keys[1]].set()
        await asyncio.wait_for(executor.started[keys[2]].wait(), timeout=1)
        assert not executor.cleaned[keys[0]].is_set()
        assert executor.peak == 2
        assert inbox.read_counts == [2, 1]
        stop.set()
        executor.release_all()
        await asyncio.wait_for(running, timeout=1)
        assert len(inbox.acknowledged) == 3
    finally:
        executor.release_all()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_stop_interrupts_an_idle_read_without_leaving_background_tasks() -> None:
    inbox = QueueInbox()
    executor = GatedExecutor(())
    stop = asyncio.Event()
    running = asyncio.create_task(WorkerService(inbox=inbox, executor=executor).run(stop))
    try:
        await asyncio.wait_for(inbox.read_started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(running, timeout=1)
        assert inbox.readers == 0
        assert inbox.acknowledged == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_service_cancellation_joins_active_executor_cleanup_without_acknowledging() -> None:
    records = tuple(dispatch_record() for _ in range(2))
    inbox = QueueInbox(records)
    executor = GatedExecutor(records)
    running = asyncio.create_task(WorkerService(
        inbox=inbox, executor=executor, batch_size=2,
    ).run(asyncio.Event()))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in executor.started.values())), timeout=1
        )
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert all(event.is_set() for event in executor.cleaned.values())
        assert executor.active == 0
        assert inbox.readers == 0
        assert inbox.acknowledged == []
    finally:
        executor.release_all()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("error", [RuntimeError("task failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_task_does_not_stop_other_leases(error: BaseException) -> None:
    records = tuple(dispatch_record() for _ in range(3))
    keys = [record.payload["task_id"] for record in records]
    inbox = QueueInbox(records)
    executor = GatedExecutor(records)
    executor.failures[keys[0]] = error
    stop = asyncio.Event()
    running = asyncio.create_task(WorkerService(
        inbox=inbox, executor=executor, batch_size=2,
    ).run(stop))
    try:
        await asyncio.wait_for(executor.started[keys[1]].wait(), timeout=1)
        executor.release[keys[0]].set()
        await asyncio.wait_for(executor.started[keys[2]].wait(), timeout=1)
        assert not executor.cleaned[keys[1]].is_set()
        stop.set()
        executor.release_all()
        await asyncio.wait_for(running, timeout=1)
        assert set(record.stream_id for record in inbox.acknowledged) == {
            records[1].stream_id, records[2].stream_id,
        }
    finally:
        executor.release_all()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_stop_does_not_start_an_envelope_returned_by_the_final_read() -> None:
    record = dispatch_record()
    stop = asyncio.Event()

    class StoppingInbox(QueueInbox):
        async def read(self, *, count: int, block_ms: int) -> tuple[RedisStreamRecord, ...]:
            stop.set()
            return (record,)

    inbox = StoppingInbox()
    executor = GatedExecutor((record,))
    await asyncio.wait_for(WorkerService(inbox=inbox, executor=executor).run(stop), timeout=1)
    assert not executor.started[record.payload["task_id"]].is_set()
    assert inbox.acknowledged == []


async def test_cancellation_during_graceful_drain_still_joins_every_executor() -> None:
    records = tuple(dispatch_record() for _ in range(2))
    inbox = QueueInbox(records)
    executor = GatedExecutor(records)
    stop = asyncio.Event()
    running = asyncio.create_task(WorkerService(
        inbox=inbox, executor=executor, batch_size=2,
    ).run(stop))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in executor.started.values())), timeout=1
        )
        stop.set()
        # Allow the runner to enter its drain before forced service cancellation.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert executor.active == 0
        assert all(event.is_set() for event in executor.cleaned.values())
        assert inbox.acknowledged == []
    finally:
        executor.release_all()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
