"""Promoted memory must be reachable by the next run.

Regression tests for a defect that made the memory subsystem a paid write-only
sink. `is_fresh` required the memory's `baseline_commit` to equal the query's.
A promoted memory records the commit the run *produced*; a query carries the
commit the run *started from*. The two are equal only when a run changed
nothing, in which case there is nothing worth remembering. So every recall
returned empty, on every run, forever, and the paid `recall` node summarised
nothing while the corpus grew.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from knowledge.memory.port import (
    ContextRequest,
    MemoryQuery,
    RetrievedMemory,
    is_fresh,
    recall_preference,
    render_context,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def memory(
    *,
    repository_id=None,
    baseline_commit: str | None = None,
    valid_until: datetime | None = None,
    observed_at: datetime | None = None,
) -> RetrievedMemory:
    return RetrievedMemory(
        memory_id=uuid4(),
        revision_id="rev-1",
        text="Cached user lookup with invalidation on write.",
        score=0.9,
        memory_type="procedural",
        source_id="memory-candidate",
        evidence_ids=("artifact-1",),
        repository_id=repository_id if repository_id is not None else uuid4(),
        baseline_commit=baseline_commit,
        observed_at=observed_at or (NOW - timedelta(days=1)),
        valid_until=valid_until,
    )


def query(
    *,
    repository_id=None,
    baseline_commit: str | None = None,
    now: datetime = NOW,
) -> MemoryQuery:
    return MemoryQuery(
        query="how should the user lookup be cached",
        project_id=uuid4(),
        repository_id=repository_id if repository_id is not None else uuid4(),
        baseline_commit=baseline_commit,
        now=now,
    )


def test_memory_from_a_prior_commit_is_reachable() -> None:
    """The regression: a memory written at the produced commit, queried at the baseline."""
    repository = uuid4()
    produced = "b" * 40
    baseline = "a" * 40

    candidate = memory(repository_id=repository, baseline_commit=produced)
    ask = query(repository_id=repository, baseline_commit=baseline)

    assert is_fresh(candidate, ask) is True


def test_an_exact_commit_match_is_still_reachable() -> None:
    repository = uuid4()
    commit = "c" * 40

    assert is_fresh(memory(repository_id=repository, baseline_commit=commit),
                    query(repository_id=repository, baseline_commit=commit)) is True


def test_memory_from_another_repository_is_still_refused() -> None:
    """The rule that actually protects against cross-repository contamination."""
    candidate = memory(repository_id=uuid4(), baseline_commit="a" * 40)
    ask = query(repository_id=uuid4(), baseline_commit="b" * 40)

    assert is_fresh(candidate, ask) is False


def test_expired_memory_is_refused() -> None:
    repository = uuid4()
    candidate = memory(
        repository_id=repository,
        baseline_commit="a" * 40,
        valid_until=NOW - timedelta(seconds=1),
    )

    assert is_fresh(candidate, query(repository_id=repository)) is False


def test_unexpired_memory_passes_its_expiry() -> None:
    repository = uuid4()
    candidate = memory(
        repository_id=repository,
        baseline_commit="a" * 40,
        valid_until=NOW + timedelta(days=1),
    )

    assert is_fresh(candidate, query(repository_id=repository)) is True


def test_unanchored_memory_is_refused_for_a_commit_scoped_query() -> None:
    repository = uuid4()

    assert is_fresh(
        memory(repository_id=repository, baseline_commit=None),
        query(repository_id=repository, baseline_commit="a" * 40),
    ) is False


def test_an_exact_commit_match_is_ranked_first() -> None:
    repository = uuid4()
    exact = memory(repository_id=repository, baseline_commit="a" * 40,
                   observed_at=NOW - timedelta(days=30))
    adjacent = memory(repository_id=repository, baseline_commit="b" * 40,
                      observed_at=NOW - timedelta(seconds=1))
    ask = query(repository_id=repository, baseline_commit="a" * 40)

    assert recall_preference(exact, ask) < recall_preference(adjacent, ask)


def test_recent_memories_outrank_distant_ones_at_the_same_distance() -> None:
    repository = uuid4()
    older = memory(repository_id=repository, baseline_commit="b" * 40,
                   observed_at=NOW - timedelta(days=10))
    newer = memory(repository_id=repository, baseline_commit="c" * 40,
                   observed_at=NOW - timedelta(days=1))
    ask = query(repository_id=repository, baseline_commit="a" * 40)

    assert recall_preference(older, ask) > recall_preference(newer, ask)


def test_recall_actually_returns_content_for_a_prior_run() -> None:
    """End to end at the port boundary: the shape a real recall takes."""
    repository = uuid4()
    candidates = (
        memory(repository_id=repository, baseline_commit="f" * 40),
        memory(repository_id=uuid4(), baseline_commit="a" * 40),
        memory(repository_id=repository, baseline_commit="a" * 40),
    )
    ask = query(repository_id=repository, baseline_commit="a" * 40)

    accepted = tuple(item for item in candidates if is_fresh(item, ask))
    accepted = tuple(sorted(accepted, key=lambda item: recall_preference(item, ask)))
    context = render_context(accepted, budget_tokens=4_000)

    assert len(accepted) == 2, "same-repository memories must survive"
    assert "Cached user lookup" in context.rendered
    assert context.tokens_used > 0


@pytest.mark.parametrize("budget", [1, 8, 32])
def test_context_packing_respects_its_budget(budget: int) -> None:
    memories = tuple(
        memory(repository_id=uuid4(), baseline_commit="a" * 40) for _ in range(20)
    )

    context = render_context(memories, budget_tokens=budget)

    assert context.tokens_used <= budget


def test_packed_context_reports_exactly_what_it_kept() -> None:
    memories = tuple(
        memory(repository_id=uuid4(), baseline_commit="a" * 40) for _ in range(20)
    )

    context = render_context(memories, budget_tokens=32)

    assert context.memories == tuple(memories[: len(context.memories)])
    assert len(context.memories) < len(memories)


def test_context_request_carries_the_fields_is_fresh_needs() -> None:
    request = ContextRequest(
        task="cache the user lookup",
        project_id=uuid4(),
        budget_tokens=1_000,
        repository_id=uuid4(),
        baseline_commit="a" * 40,
    )

    assert request.repository_id is not None
    assert request.baseline_commit == "a" * 40
