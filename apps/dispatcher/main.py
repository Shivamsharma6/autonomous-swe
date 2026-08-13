from __future__ import annotations

import asyncio
import signal
import socket
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from redis.asyncio import Redis

from domain.models import ContractModel
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService, TaskClaim
from infrastructure.config import Settings
from messaging.outbox import OutboxPublisher
from messaging.redis_streams import RedisStreamsTransport
from persistence.database import Database


class DispatchMessage(ContractModel):
    schema_version: str = "1.0"
    task_id: UUID
    project_id: UUID
    owner: str
    lease_token: UUID
    attempt_id: UUID
    expires_at: str

    @classmethod
    def from_claim(cls, claim: TaskClaim) -> DispatchMessage:
        return cls(
            task_id=claim.task_id,
            project_id=claim.project_id,
            owner=claim.owner,
            lease_token=claim.token,
            attempt_id=uuid5(NAMESPACE_URL, f"task-attempt:{claim.task_id}:{claim.token}"),
            expires_at=claim.expires_at.isoformat(),
        )


class SchedulerClaimPort(Protocol):
    async def reclaim_expired(self) -> int: ...

    async def claim_ready(self, *, owner: str, limit: int) -> tuple[TaskClaim, ...]: ...


class DispatchPublisher(Protocol):
    async def publish(self, message: DispatchMessage) -> None: ...


class RedisDispatchPublisher:
    def __init__(self, transport: RedisStreamsTransport) -> None:
        self._transport = transport

    async def publish(self, message: DispatchMessage) -> None:
        await self._transport.publish(
            "task-dispatch",
            message.lease_token,
            message.model_dump(mode="json"),
        )


class DispatcherService:
    def __init__(
        self,
        *,
        scheduler: SchedulerClaimPort,
        publisher: DispatchPublisher,
        owner: str,
        batch_size: int = 32,
        poll_seconds: float = 0.25,
    ) -> None:
        if not owner.strip() or batch_size < 1 or poll_seconds <= 0:
            raise ValueError("dispatcher owner, batch size, and poll interval must be valid")
        self._scheduler = scheduler
        self._publisher = publisher
        self._owner = owner
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds

    async def dispatch_once(self) -> int:
        await self._scheduler.reclaim_expired()
        claims = await self._scheduler.claim_ready(owner=self._owner, limit=self._batch_size)
        for claim in claims:
            await self._publisher.publish(DispatchMessage.from_claim(claim))
        return len(claims)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            dispatched = await self.dispatch_once()
            if dispatched:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass


async def run_dispatcher() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    transport = RedisStreamsTransport(redis)
    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=settings.max_parallel_tasks,
            max_parallel_tasks_per_project=settings.max_parallel_tasks_per_project,
            max_model_concurrency=settings.max_model_concurrency,
            max_sandbox_concurrency=settings.max_sandbox_concurrency,
        ),
        lease_ttl=timedelta(seconds=30),
    )
    owner = f"dispatcher:{socket.gethostname()}"
    service = DispatcherService(
        scheduler=scheduler,
        publisher=RedisDispatchPublisher(transport),
        owner=owner,
    )
    outbox = OutboxPublisher(database, transport, publisher_id=owner)
    stop = asyncio.Event()
    _install_signal_handlers(stop.set)

    async def publish_outbox() -> None:
        while not stop.is_set():
            published = await outbox.publish_batch(limit=100)
            if not published:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass

    try:
        await asyncio.gather(service.run(stop), publish_outbox())
    finally:
        await redis.aclose()
        await database.dispose()


def _install_signal_handlers(callback: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, callback)
        except NotImplementedError:
            signal.signal(signum, lambda *_: callback())


if __name__ == "__main__":
    asyncio.run(run_dispatcher())
