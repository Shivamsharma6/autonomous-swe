from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field

from domain.models import CommitSha, ContractModel, MemoryCandidate, Sha256


class MemoryError(RuntimeError):
    """Base class for explicit external-memory failures."""


class MemoryUnavailable(MemoryError):
    """UAMS cannot currently satisfy a memory-dependent operation."""


class MemoryContractError(MemoryError):
    """UAMS returned a response that violates the memory contract."""


class MemoryQuery(ContractModel):
    query: Annotated[str, Field(min_length=1, max_length=20_000)]
    project_id: UUID
    entities: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = ()
    limit: int = Field(default=10, ge=1, le=100)
    repository_id: UUID | None = None
    baseline_commit: CommitSha | None = None
    now: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ContextRequest(ContractModel):
    task: Annotated[str, Field(min_length=1, max_length=20_000)]
    project_id: UUID
    budget_tokens: int = Field(ge=1, le=100_000)
    entities: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = ()
    repository_id: UUID | None = None
    baseline_commit: CommitSha | None = None
    now: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class RetrievedMemory(ContractModel):
    memory_id: UUID
    revision_id: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=100_000)]
    score: float = Field(ge=0)
    memory_type: Annotated[str, Field(min_length=1, max_length=50)]
    source_id: Annotated[str, Field(min_length=1, max_length=2_000)]
    evidence_ids: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = Field(
        min_length=1, max_length=1_000
    )
    project_id: UUID | None = None
    repository_id: UUID | None = None
    baseline_commit: CommitSha | None = None
    observed_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    source_run_id: UUID | None = None
    source_task_id: UUID | None = None
    source_attempt_id: UUID | None = None
    source_agent: str | None = None
    artifact_hashes: tuple[Sha256, ...] = ()
    originating_message_ids: tuple[UUID, ...] = ()


class MemoryContext(ContractModel):
    memories: tuple[RetrievedMemory, ...]
    rendered: str
    tokens_used: int = Field(ge=0)


class MemoryWrite(ContractModel):
    memory_id: UUID
    candidate: MemoryCandidate
    tags: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = ()


class RememberReceipt(ContractModel):
    memory_id: UUID
    revision_id: str | None
    status: Annotated[str, Field(min_length=1, max_length=100)]
    searchable: bool
    source_id: str | None = None


class MemoryPort(Protocol):
    async def ready(self) -> bool: ...

    async def get_context(self, request: ContextRequest) -> MemoryContext: ...

    async def search(self, query: MemoryQuery) -> tuple[RetrievedMemory, ...]: ...

    async def remember(self, write: MemoryWrite) -> RememberReceipt: ...


def is_fresh(memory: RetrievedMemory, query: MemoryQuery) -> bool:
    if memory.valid_until is not None and memory.valid_until <= query.now:
        return False
    if query.repository_id is not None and query.baseline_commit is not None:
        if memory.repository_id is None or memory.baseline_commit is None:
            return False
        if memory.repository_id != query.repository_id:
            # Memories from a different repository must never pass a
            # repository+baseline scoped query.
            return False
        return memory.baseline_commit == query.baseline_commit
    return True


def render_context(memories: tuple[RetrievedMemory, ...], *, budget_tokens: int) -> MemoryContext:
    blocks: list[str] = []
    accepted: list[RetrievedMemory] = []
    tokens = 0
    for memory in memories:
        block = (
            f"Memory: {memory.memory_id}\n"
            f"Revision: {memory.revision_id}\n"
            f"Source: {memory.source_id}\n"
            f"Evidence: {', '.join(memory.evidence_ids)}\n"
            f"{memory.text}"
        )
        estimated = max(1, (len(block) + 3) // 4)
        if tokens + estimated > budget_tokens:
            continue
        blocks.append(block)
        accepted.append(memory)
        tokens += estimated
    return MemoryContext(
        memories=tuple(accepted),
        rendered="\n\n".join(blocks),
        tokens_used=tokens,
    )
