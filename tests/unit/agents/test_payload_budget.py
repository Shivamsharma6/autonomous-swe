"""The invocation payload must budget by section, not by alphabetical position.

Regression tests for a defect that silently starved every agent. The payload was
rendered with `sort_keys=True` and then cut at a character ceiling, so on any
repository large enough to fill the budget the cut landed inside `repository`
and deleted `task_type` plus every upstream handoff summary. The document still
looked well formed, so the agent proceeded without knowing what it was doing or
what it was building on, and nothing reported the loss.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.base import _MAX_PAYLOAD_CHARS, _bounded_json


def _validation_payload(
    *, dependencies: int = 10, repository_files: int = 2_000
) -> dict[str, Any]:
    return {
        "task_type": "validation",
        "agent_role": "validation",
        "execution_requirements": (
            "Verify every acceptance criterion against direct evidence. " * 12
        ),
        "acceptance_criteria": [
            "user lookup returns cached rows",
            "cache invalidates on write",
        ],
        "upstream_summaries": [
            {
                "task_id": f"task-{index}",
                "summary": "Implemented cached user lookup with invalidation on write. " * 12,
            }
            for index in range(dependencies)
        ],
        "prior_summaries": [
            {"node": "implement", "summary": "x" * 1_500} for _ in range(4)
        ],
        "repository": "src/" + "x" * 120,
        "repository_files": [f"src/pkg/module_{i:04d}.py" for i in range(repository_files)],
        "source_files": [
            {"path": f"src/pkg/module_{i:04d}.py", "content": "y" * 900}
            for i in range(repository_files)
        ],
    }


def _architect_payload(*, repository_files: int = 2_000) -> dict[str, Any]:
    return {
        "requirements": "Decompose the goal into a validated, dependency-aware task DAG. " * 14,
        "task_execution_contract": {
            "rules": ["bounded retries", "evidence before claims", "no undeclared tools"] * 30
        },
        "platform_limits": [
            {"max_dynamic_tasks": 24, "max_plan_depth": 12, "max_total_budget_usd": 25.0}
        ]
        * 20,
        "max_risk_ceiling": "MEDIUM",
        "repository_files": [f"src/pkg/module_{i:04d}.py" for i in range(repository_files)],
    }


def _rendered(payload: dict[str, Any]) -> dict[str, Any]:
    rendered = _bounded_json(payload)
    assert len(rendered) <= _MAX_PAYLOAD_CHARS + 64
    # The document must always be valid JSON with an explicit drop notice rather
    # than a raw string cut.
    return json.loads(rendered)


def test_payload_that_fits_is_returned_unchanged() -> None:
    payload = {"task_type": "implementation", "agent_role": "coder", "goal": "ship it"}

    rendered = _bounded_json(payload)

    assert json.loads(rendered) == payload


def test_task_type_survives_a_large_repository() -> None:
    document = _rendered(_validation_payload())

    assert document["task_type"] == "validation"
    assert document["agent_role"] == "validation"


def test_every_upstream_summary_survives_a_large_repository() -> None:
    document = _rendered(_validation_payload(dependencies=10))

    assert len(document["upstream_summaries"]) == 10
    assert all(
        summary["task_id"].startswith("task-")
        for summary in document["upstream_summaries"]
    )


def test_prior_summaries_survive_a_large_repository() -> None:
    document = _rendered(_validation_payload())

    assert len(document["prior_summaries"]) == 4


def test_instructions_survive_a_large_repository() -> None:
    document = _rendered(_validation_payload())

    assert "direct evidence" in document["execution_requirements"]
    assert len(document["acceptance_criteria"]) == 2


def test_architect_instruction_block_survives_a_large_repository() -> None:
    document = _rendered(_architect_payload())

    assert "validated, dependency-aware task DAG" in document["requirements"]
    assert document["task_execution_contract"]["rules"]
    assert document["platform_limits"]
    assert document["max_risk_ceiling"] == "MEDIUM"


def test_bulk_manifest_is_what_gets_reduced() -> None:
    payload = _validation_payload(repository_files=2_000)

    document = _rendered(payload)

    assert len(document["repository_files"]) < 2_000
    assert len(document["source_files"]) < 2_000


def test_the_drop_is_reported_rather_than_silent() -> None:
    document = _rendered(_validation_payload(repository_files=2_000))

    notices = [value for value in _flatten(document) if "omitted" in str(value)]
    assert notices, "a reduced payload must name what it dropped"


def test_a_small_manifest_is_not_reduced() -> None:
    document = _rendered(_validation_payload(repository_files=20, dependencies=2))

    assert len(document["repository_files"]) == 20
    assert len(document["source_files"]) == 20


def test_pathological_instructions_degrade_without_losing_keys() -> None:
    payload = {
        "requirements": "r" * 400_000,
        "task_type": "implementation",
        "repository": "src/" + "x" * 200_000,
    }

    document = _rendered(payload)

    assert document["task_type"] == "implementation"
    assert "requirements" in document


def test_every_critical_key_is_present_even_at_the_extreme() -> None:
    payload = {
        "task_type": "refactor",
        "agent_role": "coder",
        "execution_requirements": "e" * 200_000,
        "requirements": "r" * 200_000,
        "upstream_summaries": [{"summary": "s" * 50_000}],
        "prior_summaries": [{"summary": "s" * 50_000}],
        "repository_files": ["f"] * 50_000,
    }

    document = _rendered(payload)

    for key in ("task_type", "agent_role", "execution_requirements", "requirements"):
        assert key in document


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _flatten(child)]
    if isinstance(value, list):
        return [item for child in value for item in _flatten(child)]
    return [value]


def test_the_rendered_payload_is_always_valid_json() -> None:
    """Appending a notice to a truncated string yields trailing garbage.

    `json.loads` rejects `{"a":1}...[truncated]`, so the agent would fail on a
    payload the model never received, with no indication that the payload was the
    problem. Every rendering path must produce a parseable document.
    """
    payload = {
        "task_type": "implementation",
        "agent_role": "coder",
        "requirements": "r" * 500_000,
        "upstream_summaries": [{"summary": "s" * 100_000}],
        "repository_files": ["f"] * 50_000,
    }

    rendered = _bounded_json(payload)

    assert len(rendered) <= _MAX_PAYLOAD_CHARS
    parsed = json.loads(rendered)
    assert isinstance(parsed, dict)
    assert parsed["task_type"] == "implementation"


@pytest.mark.parametrize(
    "payload",
    [
        {"blob": "x" * 1_000_000},
        {"requirements": "r" * 900_000, "task_type": "validation"},
        {f"key_{index}": "v" * 20_000 for index in range(50)},
        {"nested": {"a": {"b": {"c": "d" * 400_000}}}, "task_type": "test"},
    ],
)
def test_extreme_payloads_remain_parseable(payload: dict[str, Any]) -> None:
    rendered = _bounded_json(payload)

    assert len(rendered) <= _MAX_PAYLOAD_CHARS
    assert isinstance(json.loads(rendered), dict)
