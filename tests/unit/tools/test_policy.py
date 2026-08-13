from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import Field

from domain.enums import RiskLevel
from domain.models import ContractModel
from policies.risk.policy_engine import ToolRiskPolicy
from tools.registry import (
    NetworkProfile,
    ReplayPolicy,
    SideEffectClass,
    ToolExecutionContext,
    ToolRegistrationError,
    ToolRegistry,
    ToolSpec,
)


class ReadArguments(ContractModel):
    schema_version: str = "1.0"
    path: str = Field(min_length=1, max_length=1_024)


class ReadResult(ContractModel):
    schema_version: str = "1.0"
    content: str


class UnversionedArguments(ContractModel):
    value: str


async def read_executor(arguments: ContractModel, _: ToolExecutionContext) -> dict[str, object]:
    assert isinstance(arguments, ReadArguments)
    return {"schema_version": "1.0", "content": arguments.path}


def read_spec(**overrides: object) -> ToolSpec:
    values: dict[str, object] = {
        "name": "read_file",
        "version": "1.0",
        "argument_model": ReadArguments,
        "result_model": ReadResult,
        "owning_capability": "repository.read",
        "eligible_agents": frozenset({"researcher", "coder"}),
        "base_risk": RiskLevel.LOW,
        "timeout_seconds": 5.0,
        "max_attempts": 2,
        "replay_policy": ReplayPolicy.SAFE,
        "sandbox_profile": "read-only",
        "network_profile": NetworkProfile.NONE,
        "side_effect": SideEffectClass.NONE,
        "approval_required": False,
        "path_fields": ("path",),
    }
    values.update(overrides)
    return ToolSpec(**values)  # type: ignore[arg-type]


def context(root: Path, **overrides: object) -> ToolExecutionContext:
    values: dict[str, object] = {
        "project_id": uuid4(),
        "repository_id": uuid4(),
        "run_id": uuid4(),
        "task_id": uuid4(),
        "attempt_id": uuid4(),
        "baseline_commit": "a" * 40,
        "agent_role": "researcher",
        "agent_capabilities": frozenset({"repository.read"}),
        "risk_ceiling": RiskLevel.LOW,
        "worktree_root": root,
    }
    values.update(overrides)
    return ToolExecutionContext.model_validate(values)


def test_registry_exposes_versioned_schemas_and_execution_policy() -> None:
    registry = ToolRegistry()
    registry.register(read_spec(), read_executor)

    registered = registry.resolve("read_file", "1.0")

    assert registered.argument_schema["properties"]["schema_version"]["default"] == "1.0"
    assert registered.result_schema["properties"]["schema_version"]["default"] == "1.0"
    assert registered.spec.owning_capability == "repository.read"
    assert registered.spec.eligible_agents == frozenset({"researcher", "coder"})
    assert registered.spec.timeout_seconds == 5.0
    assert registered.spec.max_attempts == 2
    assert registered.spec.retry_on_timeout is True
    assert registered.spec.retry_on_transient is True
    assert registered.spec.initial_backoff_seconds == 0.1
    assert registered.spec.replay_policy is ReplayPolicy.SAFE
    assert registered.spec.side_effect is SideEffectClass.NONE


def test_registry_forces_approval_for_protected_side_effects() -> None:
    with pytest.raises(ToolRegistrationError, match="requires approval"):
        ToolRegistry().register(
            read_spec(
                name="git_commit",
                owning_capability="repository.commit",
                side_effect=SideEffectClass.EXTERNAL,
                approval_required=False,
            ),
            read_executor,
        )


def test_registry_rejects_unversioned_contracts_and_unsafe_retry_policy() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        read_spec(argument_model=UnversionedArguments)
    with pytest.raises(ValueError, match="exactly one attempt"):
        read_spec(replay_policy=ReplayPolicy.NEVER, max_attempts=2)
    with pytest.raises(ValueError, match="backoff"):
        read_spec(initial_backoff_seconds=2, maximum_backoff_seconds=1)


def test_capability_agent_and_risk_ceiling_are_all_authoritative(tmp_path: Path) -> None:
    registered = ToolRegistry()
    registered.register(read_spec(), read_executor)
    tool = registered.resolve("read_file", "1.0")

    with pytest.raises(PermissionError, match="capability"):
        tool.authorize(context(tmp_path, agent_capabilities=frozenset()))
    with pytest.raises(PermissionError, match="eligible"):
        tool.authorize(context(tmp_path, agent_role="architect"))
    with pytest.raises(PermissionError, match="risk ceiling"):
        tool.authorize(
            context(tmp_path),
            calculated_risk=RiskLevel.HIGH,
        )


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/etc/passwd", "src/../../outside", "C:/secrets.txt"],
)
def test_path_normalization_rejects_repository_escape(tmp_path: Path, path: str) -> None:
    registry = ToolRegistry()
    registry.register(read_spec(), read_executor)
    tool = registry.resolve("read_file", "1.0")

    with pytest.raises(ValueError, match="managed worktree"):
        tool.validate_arguments({"schema_version": "1.0", "path": path}, context(tmp_path))


def test_path_normalization_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    registry = ToolRegistry()
    registry.register(read_spec(), read_executor)
    tool = registry.resolve("read_file", "1.0")

    with pytest.raises(ValueError, match="managed worktree"):
        tool.validate_arguments(
            {"schema_version": "1.0", "path": "linked/secret.txt"},
            context(tmp_path),
        )


def test_risk_combines_tool_target_arguments_and_repository_policy() -> None:
    policy = ToolRiskPolicy(
        protected_paths=(".github/workflows", "infra"),
        repository_floor=RiskLevel.MEDIUM,
    )

    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="write_file",
            arguments={"path": ".github/workflows/deploy.yml"},
            side_effect=SideEffectClass.LOCAL,
        )
        is RiskLevel.HIGH
    )
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="read_file",
            arguments={"path": "src/app.py"},
            side_effect=SideEffectClass.NONE,
        )
        is RiskLevel.MEDIUM
    )
