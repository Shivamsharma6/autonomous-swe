from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import ValidationError

from knowledge.memory.port import (
    ContextRequest,
    MemoryContext,
    MemoryContractError,
    MemoryQuery,
    MemoryUnavailable,
    MemoryWrite,
    RememberReceipt,
    RetrievedMemory,
    is_fresh,
    render_context,
)
from observability.tracing import current_correlation

_PROVENANCE = re.compile(r"<!--\s*autoswe-provenance:(\{.*?\})\s*-->")


class UAMSMemoryAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ready(self) -> bool:
        try:
            body = await self._request("GET", "/ready")
        except MemoryUnavailable:
            return False
        if body is None:
            raise MemoryContractError("UAMS /ready unexpectedly returned no body")
        return bool(body.get("ready"))

    async def search(self, query: MemoryQuery) -> tuple[RetrievedMemory, ...]:
        body = await self._request(
            "POST",
            "/search",
            json_body={
                "query": query.query,
                "limit": query.limit,
                "entities": list(query.entities),
                "projects": [str(query.project_id)],
                "compress": True,
                "max_tokens": 4_000,
            },
        )
        if body is None:
            raise MemoryContractError("UAMS /search unexpectedly returned no body")
        raw_results = body.get("results")
        if not isinstance(raw_results, list):
            raise MemoryContractError("UAMS /search response is missing results")
        results: list[RetrievedMemory] = []
        for raw in raw_results:
            if not isinstance(raw, dict) or not raw.get("memory_id") or not raw.get("revision_id"):
                continue
            try:
                record = self._parse_result(cast(dict[str, Any], raw))
            except (ValidationError, ValueError, TypeError) as error:
                raise MemoryContractError(f"invalid UAMS search result: {error}") from error
            if is_fresh(record, query):
                results.append(record)
        # Deterministic relevance order so budget packing is stable and
        # aligned with the contract-test port behaviour.
        results.sort(key=lambda record: record.score, reverse=True)
        return tuple(results)

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
        status = await self._memory_status(write.memory_id)
        if status is not None:
            return self._receipt(write.memory_id, status)
        response = await self._request(
            "POST",
            "/remember",
            json_body={
                "text": memory_document(write),
                "category": write.candidate.classification,
                "tags": list(write.tags),
                "source_agent": write.candidate.source_agent,
                "project": str(write.candidate.project_id),
            },
        )
        if response is None:
            raise MemoryContractError("UAMS /remember unexpectedly returned no body")
        returned_id = response.get("memory_id")
        if returned_id is not None and UUID(str(returned_id)) != write.memory_id:
            raise MemoryContractError("UAMS returned a different memory_id")
        status = await self._memory_status(write.memory_id)
        if status is not None:
            return self._receipt(write.memory_id, status)
        return RememberReceipt(
            memory_id=write.memory_id,
            revision_id=None,
            status=str(response.get("index_status") or "pending"),
            searchable=False,
            source_id=str(response.get("path")) if response.get("path") else None,
        )

    async def _memory_status(self, memory_id: UUID) -> dict[str, Any] | None:
        return await self._request("GET", f"/memory/status/{memory_id}", allow_not_found=True)

    def _receipt(self, memory_id: UUID, status: dict[str, Any]) -> RememberReceipt:
        current = status.get("current_revision_id")
        latest = status.get("latest_revision_id")
        searchable = bool(
            status.get("index_status") == "indexed"
            and status.get("document_status") == "active"
            and status.get("revision_state") == "active"
            and current
            and current == latest
        )
        return RememberReceipt(
            memory_id=memory_id,
            revision_id=str(current or latest) if current or latest else None,
            status=str(status.get("index_status") or "pending"),
            searchable=searchable,
            source_id=str(status.get("path")) if status.get("path") else None,
        )

    def _parse_result(self, raw: dict[str, Any]) -> RetrievedMemory:
        metadata: dict[str, Any] = (
            cast(dict[str, Any], raw["metadata"]) if isinstance(raw.get("metadata"), dict) else {}
        )
        if not metadata:
            match = _PROVENANCE.search(str(raw.get("text") or ""))
            if match is not None:
                parsed = json.loads(match.group(1))
                metadata = cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
        return RetrievedMemory(
            memory_id=raw["memory_id"],
            revision_id=str(raw["revision_id"]),
            text=str(raw["text"]),
            score=float(raw.get("score", 0.0)),
            memory_type=str(raw.get("memory_type") or "unknown"),
            source_id=str(raw.get("source_file") or raw.get("chunk_id")),
            evidence_ids=tuple(str(value) for value in raw.get("evidence_ids") or ()),
            project_id=metadata.get("project_id"),
            repository_id=metadata.get("repository_id"),
            baseline_commit=metadata.get("baseline_commit"),
            observed_at=_optional_datetime(metadata.get("observed_at")),
            verified_at=_optional_datetime(metadata.get("verified_at")),
            valid_until=_optional_datetime(metadata.get("valid_until")),
            source_run_id=metadata.get("source_run_id"),
            source_task_id=metadata.get("source_task_id"),
            source_attempt_id=metadata.get("source_attempt_id"),
            source_agent=metadata.get("source_agent"),
            artifact_hashes=tuple(metadata.get("artifact_hashes") or ()),
            originating_message_ids=tuple(metadata.get("originating_message_ids") or ()),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers | current_correlation().to_headers(),
                json=json_body,
            )
        except httpx.RequestError as error:
            raise MemoryUnavailable(f"UAMS request failed: {error}") from error
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise MemoryUnavailable(
                f"UAMS {path} returned {response.status_code}: {response.text[:500]}"
            )
        if response.status_code >= 400:
            raise MemoryContractError(
                f"UAMS {path} returned {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as error:
            raise MemoryContractError(f"UAMS {path} returned invalid JSON") from error
        if not isinstance(body, dict):
            raise MemoryContractError(f"UAMS {path} returned a non-object response")
        return cast(dict[str, Any], body)


def memory_document(write: MemoryWrite) -> str:
    candidate = write.candidate
    metadata = {
        "schema_version": candidate.schema_version,
        "project_id": str(candidate.project_id),
        "repository_id": str(candidate.repository_id),
        "baseline_commit": candidate.baseline_commit,
        "source_run_id": str(candidate.source_run_id),
        "source_task_id": str(candidate.source_task_id),
        "source_attempt_id": str(candidate.source_attempt_id),
        "source_agent": candidate.source_agent,
        "observed_at": candidate.observed_at.isoformat(),
        "verified_at": candidate.verified_at.isoformat(),
        "valid_until": candidate.valid_until.isoformat() if candidate.valid_until else None,
        "originating_message_ids": [str(value) for value in candidate.originating_message_ids],
        "artifact_hashes": list(candidate.artifact_hashes),
        "verification_commands": [list(command) for command in candidate.verification_commands],
        "confidence": candidate.confidence,
        "supersedes": [str(value) for value in candidate.supersedes],
    }
    compact_metadata = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return (
        "---\n"
        f"memory_id: {write.memory_id}\n"
        f"type: {candidate.classification}\n"
        "status: active\n"
        f"project: {candidate.project_id}\n"
        f"source_agent: {json.dumps(candidate.source_agent)}\n"
        f"autoswe: {compact_metadata}\n"
        "---\n"
        "# Verified AutoSWE Knowledge\n\n"
        f"<!-- autoswe-provenance:{compact_metadata} -->\n\n"
        f"## Summary\n{candidate.content}\n\n"
        "## Provenance\n"
        f"{json.dumps(metadata, sort_keys=True, indent=2)}\n"
    )


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
