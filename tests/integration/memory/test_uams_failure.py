from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
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
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, MemoryCandidateRow, OutboxRow
from tests.integration.messaging.helpers import seed_task


def candidate(ids: dict[str, UUID]) -> MemoryCandidate:
    now = datetime.now(UTC)
    return MemoryCandidate(
        candidate_id=uuid4(),
        project_id=ids["project_id"],
        source_run_id=ids["run_id"],
        source_task_id=ids["task_id"],
        source_attempt_id=ids["attempt_id"],
        source_agent="reviewer",
        classification="procedural",
        content="Run the targeted tests before the full suite.",
        observed_at=now,
        verified_at=now,
        repository_id=ids["repository_id"],
        baseline_commit="b" * 40,
        originating_message_ids=(uuid4(),),
        artifact_hashes=("c" * 64,),
        verification_commands=(("python", "-m", "pytest", "-q"),),
        confidence=0.95,
    )


def review() -> PromotionReview:
    return PromotionReview(
        outcome_verified=True,
        artifact_evidence_verified=True,
        verification_passed=True,
        structural_quality=0.9,
        evidence_quality=0.9,
        source_kind="distilled",
    )


async def seed_candidate(database: Any) -> tuple[dict[str, UUID], MemoryCandidate]:
    ids = await seed_task(database)
    item = candidate(ids)
    repository = DomainRepository()
    async with database.transaction() as session:
        await repository.create_memory_candidate(session, item)
        await repository.record_graph_execution(
            session,
            task_id=ids["task_id"],
            run_id=ids["run_id"],
            repository_id=ids["repository_id"],
            baseline_commit="b" * 40,
            thread_id=f"memory:{ids['task_id']}",
            state=GraphExecutionState.RUNNING,
            checkpoint_id="memory-checkpoint",
        )
    return ids, item


@pytest.mark.asyncio
async def test_uams_unavailability_is_visible_and_never_falls_back(database: Any) -> None:
    ids, item = await seed_candidate(database)
    port = FakeMemoryPort(available=False)
    service = PromotionService(database, port, PromotionGate())

    result = await service.promote(item, review())

    assert result.outcome is PromotionOutcome.WAITING_FOR_MEMORY
    assert port.remembered == {}
    async with database.transaction() as session:
        row = await session.get(MemoryCandidateRow, item.candidate_id)
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
    assert row is not None
    assert row.status == "WAITING_FOR_MEMORY"
    assert row.last_error
    assert graph is not None
    assert graph.state is GraphExecutionState.WAITING_FOR_MEMORY


class PendingThenSearchablePort(FakeMemoryPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[UUID] = []

    async def remember(self, write: Any) -> RememberReceipt:
        self.calls.append(write.memory_id)
        receipt = await super().remember(write)
        if len(self.calls) == 1:
            return receipt.model_copy(update={"searchable": False, "status": "pending"})
        return receipt


@pytest.mark.asyncio
async def test_retry_uses_same_memory_id_and_completes_only_when_revision_is_searchable(
    database: Any,
) -> None:
    ids, item = await seed_candidate(database)
    port = PendingThenSearchablePort()
    service = PromotionService(database, port, PromotionGate())

    pending = await service.promote(item, review())
    async with database.transaction() as session:
        pending_row = await session.get(MemoryCandidateRow, item.candidate_id)
        promoted_events_before = await session.scalar(
            select(OutboxRow).where(OutboxRow.topic == "memory-promoted")
        )
    assert pending.outcome is PromotionOutcome.WAITING_FOR_MEMORY
    assert pending_row is not None
    assert pending_row.uams_memory_id == port.calls[0]
    assert pending_row.uams_revision_id
    assert promoted_events_before is None

    completed = await service.promote(item, review())

    assert completed.outcome is PromotionOutcome.PROMOTED
    assert port.calls == [port.calls[0], port.calls[0]]
    async with database.transaction() as session:
        row = await session.get(MemoryCandidateRow, item.candidate_id)
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
        promoted_event = await session.scalar(
            select(OutboxRow).where(OutboxRow.topic == "memory-promoted")
        )
    assert row is not None
    assert row.status == "PROMOTED"
    assert row.promoted_at is not None
    assert row.uams_searchable_at is not None
    assert graph is not None
    assert graph.state is GraphExecutionState.RUNNING
    assert promoted_event is not None


class WriteThenCrashPort(FakeMemoryPort):
    def __init__(self) -> None:
        super().__init__()
        self.crashed = False
        self.calls: list[UUID] = []

    async def remember(self, write: Any) -> RememberReceipt:
        self.calls.append(write.memory_id)
        receipt = await super().remember(write)
        if not self.crashed:
            self.crashed = True
            raise MemoryUnavailable("worker crashed before local acknowledgement")
        return receipt


@pytest.mark.asyncio
async def test_crash_after_uams_write_retries_one_logical_memory(database: Any) -> None:
    _, item = await seed_candidate(database)
    port = WriteThenCrashPort()
    service = PromotionService(database, port, PromotionGate())

    waiting = await service.promote(item, review())
    completed = await service.promote(item, review())

    assert waiting.outcome is PromotionOutcome.WAITING_FOR_MEMORY
    assert completed.outcome is PromotionOutcome.PROMOTED
    assert port.calls[0] == port.calls[1]
    assert len(port.remembered) == 1
