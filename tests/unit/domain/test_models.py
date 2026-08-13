from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from domain.enums import (
    GRAPH_EXECUTION_TRANSITIONS,
    TASK_TRANSITIONS,
    ArtifactState,
    GraphExecutionState,
    RiskLevel,
    TaskStatus,
    TaskType,
)
from domain.models import (
    AgentSpec,
    ApprovalRequest,
    ArtifactRef,
    BudgetPolicy,
    ImplementationProposal,
    MemoryCandidate,
    PlanLimits,
    ReleaseDecision,
    ResourceEstimate,
    RetryPolicy,
    SandboxExecution,
    TaskPlan,
    TaskSpec,
    ToolCallRequest,
)


def task_spec(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": uuid4(),
        "plan_revision": 1,
        "project_id": uuid4(),
        "repository_id": uuid4(),
        "title": "Implement authenticated session endpoint",
        "description": "Add a bounded session endpoint and its regression tests.",
        "task_type": TaskType.IMPLEMENTATION,
        "dependencies": [],
        "priority": 10,
        "assigned_capability": "coder",
        "acceptance_criteria": ["Targeted tests pass", "No secret is logged"],
        "allowed_tools": ["read_file", "apply_patch", "run_tests"],
        "risk_ceiling": RiskLevel.MEDIUM,
        "expected_artifacts": ["patch", "test-report"],
        "retry_policy": RetryPolicy(max_attempts=3),
        "budget": BudgetPolicy(model_tokens=20_000, cost_usd=2.5, wall_time_seconds=900),
        "estimate": ResourceEstimate(cpu_time_ms=20_000, peak_memory_bytes=536_870_912),
    }
    values.update(overrides)
    return TaskSpec.model_validate(values)


def test_task_types_are_complete_and_stable() -> None:
    assert {item.value for item in TaskType} == {
        "RESEARCH",
        "IMPLEMENTATION",
        "TEST",
        "REFACTOR",
        "DOCUMENTATION",
        "VALIDATION",
    }


def test_scheduler_and_graph_states_are_separate() -> None:
    assert TaskStatus.RUNNING.value == GraphExecutionState.RUNNING.value
    assert TaskStatus.RUNNING is not GraphExecutionState.RUNNING
    assert GraphExecutionState.WAITING_FOR_APPROVAL not in TaskStatus
    assert TaskStatus.BLOCKED not in GraphExecutionState


def test_transition_tables_reject_terminal_regression() -> None:
    assert TaskStatus.RUNNING in TASK_TRANSITIONS[TaskStatus.LEASED]
    assert TaskStatus.READY not in TASK_TRANSITIONS[TaskStatus.COMPLETED]
    assert (
        GraphExecutionState.WAITING_FOR_TOOL
        in GRAPH_EXECUTION_TRANSITIONS[GraphExecutionState.RUNNING]
    )
    assert not GRAPH_EXECUTION_TRANSITIONS[GraphExecutionState.COMPLETED]


def test_task_contract_is_immutable_and_forbids_unknown_fields() -> None:
    task = task_spec()

    with pytest.raises(ValidationError, match="frozen"):
        task.id = uuid4()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TaskSpec.model_validate(task.model_dump() | {"unbounded_prompt": "no"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_tokens", -1),
        ("cost_usd", -0.1),
        ("wall_time_seconds", -1),
        ("cpu_time_ms", -1),
        ("network_requests", -1),
    ],
)
def test_budget_values_cannot_be_negative(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        BudgetPolicy.model_validate({field: value})


def test_task_plan_carries_revision_limits_and_baseline() -> None:
    task = task_spec()
    run_id = uuid4()
    plan = TaskPlan(
        run_id=run_id,
        project_id=task.project_id,
        repository_id=task.repository_id,
        baseline_commit="a" * 40,
        revision=1,
        tasks=[task],
        limits=PlanLimits(
            max_dynamic_tasks=12,
            max_plan_depth=8,
            max_total_budget_usd=20,
            max_total_execution_seconds=3_600,
        ),
    )

    assert plan.schema_version == "1.0"
    assert plan.tasks == (task,)
    assert plan.run_id == run_id


def test_agent_spec_hash_changes_with_policy() -> None:
    spec = AgentSpec(
        role="coder",
        purpose="Implement a typed task",
        input_schema="TaskExecutionInput@1.0",
        output_schema="ImplementationProposal@1.0",
        primary_model="qwen-coder",
        fallback_models=["nemotron"],
        tool_grants=["read_file", "apply_patch"],
        maximum_risk=RiskLevel.MEDIUM,
        memory_policy="project-and-procedure",
        token_budget=8_000,
        cost_budget_usd=1.5,
        turn_budget=12,
        wall_time_seconds=600,
        sandbox_profile="python-default",
        network_profile="none",
        retry_policy="bounded-schema-repair",
        escalation_policy="human-on-policy",
        termination_policy="acceptance-or-budget",
    )

    assert len(spec.spec_hash) == 64
    changed = spec.model_copy(update={"tool_grants": ("read_file",)})
    assert changed.spec_hash != spec.spec_hash


def test_structured_proposal_and_release_decision_reference_evidence() -> None:
    patch = ArtifactRef(artifact_id=uuid4(), sha256="a" * 64, media_type="text/x-diff")
    proposal = ImplementationProposal(
        summary="Add the authenticated endpoint",
        patch=patch,
        changed_paths=["src/session.py"],
        verification_commands=[["python", "-m", "pytest", "tests/test_session.py"]],
    )
    release = ReleaseDecision(
        approved=True,
        summary="All criteria are verified",
        acceptance_evidence={"Targeted tests pass": [patch.artifact_id]},
        failure_reasons=[],
    )

    assert proposal.patch.sha256 == "a" * 64
    assert release.approved is True


def test_sandbox_execution_records_actual_usage_and_measurement_quality() -> None:
    execution = SandboxExecution(
        execution_id=uuid4(),
        task_id=uuid4(),
        cpu_time_ms=1_250,
        peak_memory_bytes=64_000_000,
        peak_processes=4,
        processes_created=None,
        stdout_bytes=3_000,
        stderr_bytes=20,
        duration_ms=2_000,
        network_requests=0,
        network_bytes_sent=0,
        network_bytes_received=0,
        exit_code=0,
        exit_reason="COMPLETED",
        limit_triggered=None,
        measurement_source="docker-cgroups-v2",
        measurement_complete=False,
    )

    assert execution.processes_created is None
    assert execution.measurement_complete is False


def test_memory_candidate_requires_freshness_and_provenance() -> None:
    now = datetime.now(UTC)
    candidate = MemoryCandidate(
        candidate_id=uuid4(),
        project_id=uuid4(),
        source_run_id=uuid4(),
        source_task_id=uuid4(),
        source_attempt_id=uuid4(),
        source_agent="reviewer",
        classification="procedural",
        content="Run the repository's targeted typecheck before its full test suite.",
        observed_at=now,
        verified_at=now,
        repository_id=uuid4(),
        baseline_commit="b" * 40,
        originating_message_ids=[uuid4()],
        artifact_hashes=["c" * 64],
        verification_commands=[["python", "-m", "pytest", "-q"]],
        confidence=0.95,
    )

    assert candidate.verified_at.tzinfo is not None
    assert candidate.artifact_hashes == ("c" * 64,)


def test_approval_hash_binds_normalized_call_repository_and_baseline() -> None:
    call = ToolCallRequest(
        call_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        requested_by="final-reviewer",
        tool_name="git_commit",
        arguments={"message": "feat: add session endpoint"},
        idempotency_key="commit:run:task:1",
    )
    approval = ApprovalRequest(
        approval_id=uuid4(),
        call=call,
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="d" * 40,
        expires_at=datetime.now(UTC),
    )

    assert len(approval.call_hash) == 64
    assert approval.call_hash != approval.model_copy(update={"baseline_commit": "e" * 40}).call_hash


def test_artifact_state_contains_corruption_terminal() -> None:
    assert ArtifactState.CORRUPT.value == "CORRUPT"
    assert isinstance(UUID(str(uuid4())), UUID)
