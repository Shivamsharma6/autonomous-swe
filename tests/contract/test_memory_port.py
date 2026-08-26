from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from domain.models import MemoryCandidate
from knowledge.memory.fake import FakeMemoryPort
from knowledge.memory.port import (
    ContextRequest,
    MemoryQuery,
    MemoryWrite,
    RetrievedMemory,
)
from knowledge.memory.uams import UAMSMemoryAdapter


def candidate(*, memory_id: UUID | None = None) -> tuple[UUID, MemoryCandidate]:
    now = datetime.now(UTC)
    resolved_id = memory_id or uuid4()
    return resolved_id, MemoryCandidate(
        candidate_id=uuid4(),
        project_id=uuid4(),
        source_run_id=uuid4(),
        source_task_id=uuid4(),
        source_attempt_id=uuid4(),
        source_agent="reviewer",
        classification="procedural",
        content="Run the targeted typecheck before the complete test suite.",
        observed_at=now,
        verified_at=now,
        repository_id=uuid4(),
        baseline_commit="a" * 40,
        originating_message_ids=(uuid4(),),
        artifact_hashes=("b" * 64,),
        verification_commands=(("python", "-m", "pytest", "-q"),),
        confidence=0.95,
    )


async def assert_memory_contract(
    port: Any, prepared: tuple[UUID, MemoryCandidate] | None = None
) -> None:
    assert await port.ready() is True
    memory_id, item = prepared or candidate()
    first = await port.remember(MemoryWrite(memory_id=memory_id, candidate=item))
    second = await port.remember(MemoryWrite(memory_id=memory_id, candidate=item))

    assert first.memory_id == memory_id
    assert second.memory_id == memory_id
    assert second.revision_id == first.revision_id
    assert second.searchable is True

    query = MemoryQuery(
        query="targeted typecheck",
        project_id=item.project_id,
        repository_id=item.repository_id,
        baseline_commit=item.baseline_commit,
        limit=5,
    )
    results = await port.search(query)
    assert len(results) == 1
    assert results[0].memory_id == memory_id
    assert results[0].revision_id == first.revision_id
    assert results[0].source_id
    assert results[0].evidence_ids

    context = await port.get_context(
        ContextRequest(
            task="Implement the verified change",
            project_id=item.project_id,
            budget_tokens=500,
            repository_id=item.repository_id,
            baseline_commit=item.baseline_commit,
        )
    )
    assert context.memories[0].memory_id == memory_id
    assert "targeted typecheck" in context.rendered.casefold()
    assert context.tokens_used <= 500


@pytest.mark.asyncio
async def test_in_memory_adapter_satisfies_contract() -> None:
    await assert_memory_contract(FakeMemoryPort())


@pytest.mark.asyncio
async def test_http_uams_adapter_satisfies_contract_and_idempotent_remember() -> None:
    memory_id, item = candidate()
    revision_id = str(uuid4())
    writes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        assert request.headers["authorization"] == "Bearer uams-test-token"
        if request.method == "GET" and request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if request.method == "GET" and request.url.path == f"/memory/status/{memory_id}":
            if writes == 0:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={
                    "memory_id": str(memory_id),
                    "current_revision_id": revision_id,
                    "latest_revision_id": revision_id,
                    "revision_state": "active",
                    "document_status": "active",
                    "index_status": "indexed",
                },
            )
        if request.method == "POST" and request.url.path == "/remember":
            writes += 1
            body = _json(request)
            assert f"memory_id: {memory_id}" in body["text"]
            assert body["project"] == str(item.project_id)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "memory_id": str(memory_id),
                    "indexed": True,
                    "index_status": "pending",
                },
            )
        if request.method == "POST" and request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "query": "targeted typecheck",
                    "intent": "procedural",
                    "expanded_entities": [],
                    "results": [
                        {
                            "chunk_id": "chunk-1",
                            "text": item.content,
                            "score": 0.94,
                            "importance": 0.8,
                            "source_file": "Tasks/verified-procedure.md",
                            "entities": [],
                            "memory_id": str(memory_id),
                            "revision_id": revision_id,
                            "memory_type": "procedural",
                            "evidence_ids": [f"{memory_id}:{revision_id}:chunk-1"],
                            "metadata": {
                                "project_id": str(item.project_id),
                                "repository_id": str(item.repository_id),
                                "baseline_commit": item.baseline_commit,
                                "observed_at": item.observed_at.isoformat(),
                                "verified_at": item.verified_at.isoformat(),
                                "source_run_id": str(item.source_run_id),
                                "source_task_id": str(item.source_task_id),
                                "source_attempt_id": str(item.source_attempt_id),
                                "source_agent": item.source_agent,
                                "artifact_hashes": list(item.artifact_hashes),
                                "originating_message_ids": [
                                    str(value) for value in item.originating_message_ids
                                ],
                            },
                        }
                    ],
                    "context_tokens_used": 40,
                },
            )
        raise AssertionError(f"unexpected UAMS request: {request.method} {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://uams.test")
    port = UAMSMemoryAdapter(
        base_url="http://uams.test",
        token="uams-test-token",  # noqa: S106 - disposable contract credential
        client=client,
    )
    try:
        await assert_memory_contract(port, (memory_id, item))
    finally:
        await client.aclose()
    assert writes == 1


@pytest.mark.asyncio
async def test_search_rejects_expired_and_stale_commit_scoped_memory() -> None:
    now = datetime.now(UTC)
    project_id, repository_id = uuid4(), uuid4()
    current = RetrievedMemory(
        memory_id=uuid4(),
        revision_id=str(uuid4()),
        text="Current procedure",
        score=0.9,
        memory_type="procedural",
        source_id="Tasks/current.md",
        evidence_ids=("evidence-current",),
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="c" * 40,
        verified_at=now,
    )
    expired = current.model_copy(
        update={"memory_id": uuid4(), "valid_until": now - timedelta(seconds=1)}
    )
    stale = current.model_copy(update={"memory_id": uuid4(), "baseline_commit": "d" * 40})
    port = FakeMemoryPort(seed=(current, expired, stale))

    results = await port.search(
        MemoryQuery(
            query="procedure",
            project_id=project_id,
            repository_id=repository_id,
            baseline_commit="c" * 40,
            now=now,
        )
    )

    assert results == (current,)


async def test_search_rejects_memories_from_other_repositories() -> None:
    now = datetime.now(UTC)
    project_id, repository_id = uuid4(), uuid4()
    foreign_repository = uuid4()
    current = RetrievedMemory(
        memory_id=uuid4(),
        revision_id=str(uuid4()),
        text="Current procedure",
        score=0.9,
        memory_type="procedural",
        source_id="Tasks/current.md",
        evidence_ids=("evidence-current",),
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="c" * 40,
        verified_at=now,
    )
    foreign = current.model_copy(
        update={"memory_id": uuid4(), "repository_id": foreign_repository}
    )
    unscoped = current.model_copy(update={"memory_id": uuid4(), "repository_id": None})
    port = FakeMemoryPort(seed=(current, foreign, unscoped))

    results = await port.search(
        MemoryQuery(
            query="procedure",
            project_id=project_id,
            repository_id=repository_id,
            baseline_commit="c" * 40,
            now=now,
        )
    )

    assert results == (current,)


def _json(request: httpx.Request) -> dict[str, Any]:
    import json

    return dict(json.loads(request.content))
