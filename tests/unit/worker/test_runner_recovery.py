import asyncio
from unittest.mock import AsyncMock

from redis.exceptions import ConnectionError as RedisConnectionError

from apps.worker.runner import RedisDispatchInbox, WorkerService


async def test_worker_recovers_after_transport_or_task_failure():
    stop = asyncio.Event()
    worker = WorkerService(inbox=AsyncMock(), executor=AsyncMock(), block_ms=1)
    calls = 0

    async def process_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RedisConnectionError("Redis unavailable")
        if calls == 2:
            raise RuntimeError("one task failed")
        stop.set()
        return 1

    worker.process_once = process_once
    await asyncio.wait_for(worker.run(stop), timeout=3)
    assert calls == 3


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
