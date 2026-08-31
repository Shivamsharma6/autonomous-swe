from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from domain.enums import RiskLevel
from domain.models import CommitSha, ContractModel
from policies.risk.policy_engine import risk_exceeds

ToolExecutor = Callable[[ContractModel, "ToolExecutionContext"], Awaitable[dict[str, Any]]]


class ReplayPolicy(StrEnum):
    NEVER = "NEVER"
    SAFE = "SAFE"
    IDEMPOTENT = "IDEMPOTENT"


class SideEffectClass(StrEnum):
    NONE = "none"
    LOCAL = "local"
    EXTERNAL = "external"


class NetworkProfile(StrEnum):
    NONE = "none"
    DEPENDENCY_EGRESS = "dependency-egress"
    PROVIDER_EGRESS = "provider-egress"


class ToolExecutionContext(ContractModel):
    project_id: UUID
    repository_id: UUID
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    baseline_commit: CommitSha
    agent_role: str = Field(min_length=1, max_length=100)
    agent_capabilities: frozenset[str] = Field(max_length=100)
    risk_ceiling: RiskLevel
    worktree_root: Path
    tool_call_id: UUID | None = None

    @field_validator("worktree_root")
    @classmethod
    def absolute_worktree(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("worktree_root must be absolute")
        return value


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    argument_model: type[ContractModel]
    result_model: type[ContractModel]
    owning_capability: str
    eligible_agents: frozenset[str]
    base_risk: RiskLevel
    timeout_seconds: float
    max_attempts: int
    replay_policy: ReplayPolicy
    sandbox_profile: str
    network_profile: NetworkProfile
    side_effect: SideEffectClass
    approval_required: bool
    path_fields: tuple[str, ...] = ()
    retry_on_timeout: bool = True
    retry_on_transient: bool = True
    initial_backoff_seconds: float = 0.1
    maximum_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.name):
            raise ValueError("invalid tool name")
        if not re.fullmatch(r"\d+\.\d+", self.version):
            raise ValueError("tool version must be major.minor")
        if not self.owning_capability or not self.eligible_agents:
            raise ValueError("tool capability and eligible agents are required")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3_600:
            raise ValueError("tool timeout must be in (0, 3600]")
        if self.max_attempts < 1 or self.max_attempts > 20:
            raise ValueError("tool max_attempts must be in [1, 20]")
        if self.replay_policy is ReplayPolicy.NEVER and self.max_attempts != 1:
            raise ValueError("non-replayable tools must have exactly one attempt")
        if (
            self.initial_backoff_seconds < 0
            or self.maximum_backoff_seconds < self.initial_backoff_seconds
            or self.maximum_backoff_seconds > 300
        ):
            raise ValueError("tool retry backoff is invalid")
        for contract in (self.argument_model, self.result_model):
            field = contract.model_fields.get("schema_version")
            if field is None or str(field.default) != self.version:
                raise ValueError(
                    f"{contract.__name__} schema_version must equal tool version {self.version}"
                )


class ToolRegistrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    executor: ToolExecutor

    @property
    def argument_schema(self) -> dict[str, Any]:
        return self.spec.argument_model.model_json_schema()

    @property
    def result_schema(self) -> dict[str, Any]:
        return self.spec.result_model.model_json_schema()

    def authorize(
        self,
        context: ToolExecutionContext,
        *,
        calculated_risk: RiskLevel | None = None,
    ) -> None:
        if self.spec.owning_capability not in context.agent_capabilities:
            raise PermissionError(f"agent lacks owning capability {self.spec.owning_capability}")
        if context.agent_role not in self.spec.eligible_agents:
            raise PermissionError(f"agent role {context.agent_role} is not eligible")
        risk = calculated_risk or self.spec.base_risk
        if risk_exceeds(risk, context.risk_ceiling):
            raise PermissionError(f"calculated {risk.value} risk exceeds agent risk ceiling")

    def validate_arguments(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ContractModel:
        normalized = dict(arguments)
        for field in self.spec.path_fields:
            value = normalized.get(field)
            if value is None:
                continue
            if isinstance(value, str):
                normalized[field] = normalize_worktree_path(value, context.worktree_root)
            elif isinstance(value, list):
                if not all(isinstance(item, str) for item in value):
                    raise ValueError(f"path field {field} must contain only strings")
                normalized[field] = [
                    normalize_worktree_path(item, context.worktree_root) for item in value
                ]
            else:
                raise ValueError(f"path field {field} must be a string or string list")
        return self.spec.argument_model.model_validate(normalized)

    def validate_result(self, result: dict[str, Any]) -> ContractModel:
        return self.spec.result_model.model_validate(result)


_MANDATORY_APPROVAL_TOOLS = {
    "git_commit",
    "git_push",
    "create_pull_request",
    "apply_infrastructure",
    "deploy",
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}

    def register(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        if _approval_is_mandatory(spec.name) and not spec.approval_required:
            raise ToolRegistrationError(f"tool {spec.name} requires approval")
        key = (spec.name, spec.version)
        if key in self._tools:
            raise ToolRegistrationError(f"tool {spec.name}@{spec.version} already registered")
        self._tools[key] = RegisteredTool(spec=spec, executor=executor)

    def resolve(self, name: str, version: str) -> RegisteredTool:
        try:
            return self._tools[(name, version)]
        except KeyError as error:
            raise LookupError(f"tool {name}@{version} is not registered") from error


def _approval_is_mandatory(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _MANDATORY_APPROVAL_TOOLS or any(
        marker in lowered
        for marker in (
            "git_commit",
            "git_push",
            "pull_request",
            "deploy",
            "infrastructure_apply",
            "terraform_apply",
        )
    )


def normalize_worktree_path(value: str, root: Path) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError("path escapes the managed worktree")
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root.joinpath(*path.parts).resolve(strict=False)
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("path escapes the managed worktree")
    return path.as_posix()
