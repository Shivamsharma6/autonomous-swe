from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from domain.models import canonical_sha256
from knowledge.memory.port import (
    ContextRequest,
    MemoryContext,
    MemoryQuery,
    MemoryUnavailable,
    MemoryWrite,
    RememberReceipt,
    RetrievedMemory,
    is_fresh,
    render_context,
)


class FakeMemoryPort:
    """Deterministic contract adapter; never used as a production fallback."""

    def __init__(
        self,
        *,
        available: bool = True,
        seed: tuple[RetrievedMemory, ...] = (),
    ) -> None:
        self.available = available
        self.remembered: dict[UUID, MemoryWrite] = {}
        self._records: dict[UUID, RetrievedMemory] = {record.memory_id: record for record in seed}

    async def ready(self) -> bool:
        return self.available

    async def search(self, query: MemoryQuery) -> tuple[RetrievedMemory, ...]:
        self._require_available()
        records = (
            record
            for record in self._records.values()
            if record.project_id in (None, query.project_id) and is_fresh(record, query)
        )
        return tuple(sorted(records, key=lambda item: item.score, reverse=True))[: query.limit]

    async def get_context(self, request: ContextRequest) -> MemoryContext:
        results = await self.search(
            MemoryQuery(
                query=request.task,
                project_id=request.project_id,
                entities=request.entities,
                limit=100,
                repository_id=request.repository_id,
                baseline_commit=request.baseline_commit,
                now=request.now,
            )
        )
        return render_context(results, budget_tokens=request.budget_tokens)

    async def remember(self, write: MemoryWrite) -> RememberReceipt:
        self._require_available()
        existing = self._records.get(write.memory_id)
        if existing is not None:
            return RememberReceipt(
                memory_id=existing.memory_id,
                revision_id=existing.revision_id,
                status="indexed",
                searchable=True,
                source_id=existing.source_id,
            )
        candidate = write.candidate
        revision_id = str(
            uuid5(
                NAMESPACE_URL,
                f"uams-revision:{write.memory_id}:{canonical_sha256(candidate.content)}",
            )
        )
        source_id = f"uams://memory/{write.memory_id}"
        record = RetrievedMemory(
            memory_id=write.memory_id,
            revision_id=revision_id,
            text=candidate.content,
            score=1.0,
            memory_type=candidate.classification,
            source_id=source_id,
            evidence_ids=(f"{write.memory_id}:{revision_id}:candidate",),
            project_id=candidate.project_id,
            repository_id=candidate.repository_id,
            baseline_commit=candidate.baseline_commit,
            observed_at=candidate.observed_at,
            verified_at=candidate.verified_at,
            valid_until=candidate.valid_until,
            source_run_id=candidate.source_run_id,
            source_task_id=candidate.source_task_id,
            source_attempt_id=candidate.source_attempt_id,
            source_agent=candidate.source_agent,
            artifact_hashes=candidate.artifact_hashes,
            originating_message_ids=candidate.originating_message_ids,
        )
        self.remembered[write.memory_id] = write
        self._records[write.memory_id] = record
        return RememberReceipt(
            memory_id=write.memory_id,
            revision_id=revision_id,
            status="indexed",
            searchable=True,
            source_id=source_id,
        )

    def _require_available(self) -> None:
        if not self.available:
            raise MemoryUnavailable("external UAMS is unavailable")
