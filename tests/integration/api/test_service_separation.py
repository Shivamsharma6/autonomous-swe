from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from apps.api.dependencies import ControlPlaneServices, ReadinessChecks
from apps.api.main import create_app
from apps.dispatcher.main import DispatcherService, DispatchMessage
from apps.worker.runner import WorkerOutcome, WorkerService
from execution.scheduler.service import TaskClaim
from infrastructure.config import Settings
from knowledge.memory.fake import FakeMemoryPort
from messaging.redis_streams import RedisStreamRecord


class FakeScheduler:
    def __init__(self, claims: tuple[TaskClaim, ...]) -> None:
        self.claims = claims
        self.reclaimed = 0
        self.claimed = 0

    async def reclaim_expired(self) -> int:
        self.reclaimed += 1
        return 0

    async def claim_ready(self, *, owner: str, limit: int) -> tuple[TaskClaim, ...]:
        self.claimed += 1
        assert owner == "dispatcher-test"
        return self.claims[:limit]


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[DispatchMessage] = []

    async def publish(self, message: DispatchMessage) -> None:
        self.messages.append(message)


class FakeInbox:
    def __init__(self, records: tuple[RedisStreamRecord, ...]) -> None:
        self.records = records
        self.acknowledged: list[str] = []

    async def read(self, *, count: int, block_ms: int) -> tuple[RedisStreamRecord, ...]:
        return self.records[:count]

    async def acknowledge(self, record: RedisStreamRecord) -> bool:
        self.acknowledged.append(record.stream_id)
        return True


class FakeExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[DispatchMessage] = []
        self.fail = fail

    async def execute(self, message: DispatchMessage) -> WorkerOutcome:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("worker stopped at a replay-safe boundary")
        return WorkerOutcome.COMPLETED


@pytest.mark.asyncio
async def test_dispatcher_claims_leases_and_worker_executes_only_dispatch_records() -> None:
    claim = TaskClaim(
        task_id=uuid4(),
        project_id=uuid4(),
        owner="dispatcher-test",
        token=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    scheduler = FakeScheduler((claim,))
    publisher = FakePublisher()
    dispatched = await DispatcherService(
        scheduler=scheduler,
        publisher=publisher,
        owner="dispatcher-test",
    ).dispatch_once()

    assert dispatched == 1
    assert scheduler.reclaimed == scheduler.claimed == 1
    assert publisher.messages[0].lease_token == claim.token
    record = RedisStreamRecord(
        stream="task-dispatch",
        stream_id="1-0",
        event_id=claim.token,
        topic="task-dispatch",
        payload=publisher.messages[0].model_dump(mode="json"),
    )
    inbox = FakeInbox((record,))
    executor = FakeExecutor()

    assert await WorkerService(inbox=inbox, executor=executor).process_once() == 1
    assert executor.messages == publisher.messages
    assert inbox.acknowledged == ["1-0"]


@pytest.mark.asyncio
async def test_worker_failure_preserves_unacknowledged_dispatch_for_resume() -> None:
    message = DispatchMessage(
        task_id=uuid4(),
        project_id=uuid4(),
        owner="dispatcher-test",
        lease_token=uuid4(),
        attempt_id=uuid4(),
        expires_at=datetime.now(UTC).isoformat(),
    )
    record = RedisStreamRecord(
        stream="task-dispatch",
        stream_id="2-0",
        event_id=message.lease_token,
        topic="task-dispatch",
        payload=message.model_dump(mode="json"),
    )
    inbox = FakeInbox((record,))

    with pytest.raises(RuntimeError, match="replay-safe"):
        await WorkerService(inbox=inbox, executor=FakeExecutor(fail=True)).process_once()

    assert inbox.acknowledged == []


def test_api_factory_starts_no_dispatcher_or_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("API factory must not create background tasks")

    monkeypatch.setattr(asyncio, "create_task", forbidden)
    config = Settings.model_validate(
        {
            "autoswe_env": "test",
            "admin_token": "q" * 40,
            "database_url": "test://postgres",
            "redis_url": "test://redis",
            "uams_url": "test://uams",
            "model_base_url": "test://model",
            "model_primary": "scripted",
            "cors_origins": ["https://console.example"],
            "python_runner_image": "test://python",
            "node_runner_image": "test://node",
        }
    )

    async def ready() -> bool:
        return True

    create_app(
        ControlPlaneServices(
            settings=config,
            database=cast(Any, None),
            redis=cast(Any, None),
            memory=FakeMemoryPort(),
            approvals=cast(Any, None),
            artifacts=cast(Any, None),
            scheduler=cast(Any, None),
            cancel_notify=cast(Any, None),
            readiness=ReadinessChecks(
                postgres=ready,
                redis=ready,
                checkpoints=ready,
                sandbox=ready,
                model=ready,
                uams=ready,
            ),
        )
    )

    assert calls == 0
