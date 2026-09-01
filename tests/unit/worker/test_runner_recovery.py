import asyncio
from unittest.mock import AsyncMock

from redis.exceptions import ConnectionError as RedisConnectionError

from apps.worker.runner import RedisDispatchInbox, WorkerOutcome, WorkerService
from tests.unit.worker.test_runner_concurrency import dispatch_record


async def test_worker_recovers_after_transport_or_task_failure():
    stop = asyncio.Event()
    failed, successful = dispatch_record(), dispatch_record()
    inbox = AsyncMock()
    inbox.read.side_effect = [RedisConnectionError("Redis unavailable"), (failed,), (successful,)]
    executor = AsyncMock()

    async def execute(message):
        if str(message.task_id) == failed.payload["task_id"]:
            raise RuntimeError("one task failed")
        stop.set()
        return WorkerOutcome.COMPLETED

    executor.execute.side_effect = execute
    worker = WorkerService(inbox=inbox, executor=executor, block_ms=1)
    await asyncio.wait_for(worker.run(stop), timeout=3)
    assert inbox.read.await_count == 3
    assert executor.execute.await_count == 2
    inbox.acknowledge.assert_awaited_once_with(successful)


async def test_inbox_recreates_consumer_group_after_redis_loss():
    transport = AsyncMock()
    transport.read.side_effect = [RedisConnectionError("lost state"), ()]
    inbox = RedisDispatchInbox(transport, group="autoswe-workers", consumer="worker")
    try:
        await inbox.read(count=1, block_ms=1)
    except RedisConnectionError:
        pass
    await inbox.read(count=1, block_ms=1)
    assert transport.ensure_group.await_count == 2
