from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from domain.enums import GraphExecutionState
from domain.models import MemoryCandidate
from knowledge.memory.fake import FakeMemoryPort
from knowledge.memory.port import MemoryUnavailable, RememberReceipt
from knowledge.memory.promotion import (
    PromotionGate,
    PromotionOutcome,
    PromotionReview,
    PromotionService,
)
from messaging.outbox import OutboxPublisher
from messaging.redis_streams import RedisStreamsTransport
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, MemoryCandidateRow, OutboxRow
from tests.integration.messaging.helpers import event_payload, seed_task


@pytest.mark.asyncio
async def test_redis_loss_recovers_from_postgres_without_changing_event_identity(
    database: Any, redis_client: Redis
) -> None:
    ids = await seed_task(database)
    event_id = uuid4()
    async with database.transaction() as session:
        await DomainRepository().enqueue_event(
            session,
            event_id=event_id,
            topic="failure-recovery",
            payload=event_payload(ids, event_id),
        )
    publisher = OutboxPublisher(
        database,
        RedisStreamsTransport(redis_client),
        publisher_id="failure-recovery",
    )

    assert await publisher.publish_batch(limit=1) == 1
    await redis_client.flushdb()
    assert await publisher.requeue(event_id)
    assert await publisher.publish_batch(limit=1) == 1

    entries = await redis_client.xrange("failure-recovery")
    assert entries is not None
    assert len(entries) == 1
    assert entries[0][1] is not None
    assert entries[0][1]["event_id"] == str(event_id)
    async with database.sessions() as session:
        row = await session.get(OutboxRow, event_id)
    assert row is not None and row.published_at is not None and row.attempts == 2


class WriteThenCrashPort(FakeMemoryPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[UUID] = []
        self._crashed = False

    async def remember(self, write: Any) -> RememberReceipt:
        self.calls.append(write.memory_id)
        receipt = await super().remember(write)
        if not self._crashed:
            self._crashed = True
            raise MemoryUnavailable("response lost after UAMS accepted the write")
        return receipt


@pytest.mark.asyncio
async def test_uams_crash_retries_same_memory_and_resumes_visible_wait(database: Any) -> None:
    ids = await seed_task(database)
    now = datetime.now(UTC)
    candidate = MemoryCandidate(
        candidate_id=uuid4(),
        project_id=ids["project_id"],
        source_run_id=ids["run_id"],
        source_task_id=ids["task_id"],
        source_attempt_id=ids["attempt_id"],
        source_agent="reviewer",
        classification="procedural",
        content="Run targeted verification before the complete suite.",
        observed_at=now,
        verified_at=now,
        repository_id=ids["repository_id"],
        baseline_commit="b" * 40,
        originating_message_ids=(uuid4(),),
        artifact_hashes=("a" * 64,),
        verification_commands=(("python", "-m", "pytest", "-q"),),
        confidence=0.95,
    )
    repository = DomainRepository()
    async with database.transaction() as session:
        await repository.create_memory_candidate(session, candidate)
        await repository.record_graph_execution(
            session,
            task_id=ids["task_id"],
            run_id=ids["run_id"],
            repository_id=ids["repository_id"],
            baseline_commit="b" * 40,
            thread_id=f"run:{ids['run_id']}:task:{ids['task_id']}",
            state=GraphExecutionState.RUNNING,
            checkpoint_id="before-uams",
        )
    review = PromotionReview(
        outcome_verified=True,
        artifact_evidence_verified=True,
        verification_passed=True,
        structural_quality=0.9,
        evidence_quality=0.9,
        source_kind="distilled",
    )
    port = WriteThenCrashPort()
    service = PromotionService(database, port, PromotionGate())

    waiting = await service.promote(candidate, review)
    completed = await service.promote(candidate, review)

    assert waiting.outcome is PromotionOutcome.WAITING_FOR_MEMORY
    assert completed.outcome is PromotionOutcome.PROMOTED
    assert port.calls[0] == port.calls[1]
    assert len(port.remembered) == 1
    async with database.sessions() as session:
        row = await session.get(MemoryCandidateRow, candidate.candidate_id)
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
    assert row is not None and row.status == "PROMOTED"
    assert graph is not None and graph.state is GraphExecutionState.RUNNING
