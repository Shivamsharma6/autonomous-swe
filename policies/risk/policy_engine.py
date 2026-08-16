import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from domain.enums import RiskLevel

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def maximum_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=_RISK_ORDER.__getitem__)


def risk_exceeds(level: RiskLevel, ceiling: RiskLevel) -> bool:
    return _RISK_ORDER[level] > _RISK_ORDER[ceiling]


@dataclass(frozen=True, slots=True)
class ToolRiskPolicy:
    protected_paths: tuple[str, ...] = (
        ".github/workflows",
        ".git",
        "infra",
        "infrastructure",
        "terraform",
    )
    repository_floor: RiskLevel = RiskLevel.LOW

    def calculate(
        self,
        *,
        base: RiskLevel,
        tool_name: str,
        arguments: Mapping[str, Any],
        side_effect: object,
    ) -> RiskLevel:
        levels = [base, self.repository_floor]
        effect = str(getattr(side_effect, "value", side_effect)).casefold()
        if effect == "local":
            levels.append(RiskLevel.MEDIUM)
        elif effect == "external":
            levels.append(RiskLevel.HIGH)

        paths = _argument_paths(arguments)
        if any(_is_protected_path(path, self.protected_paths) for path in paths):
            levels.append(RiskLevel.HIGH)
        lowered = tool_name.casefold()
        if any(value in lowered for value in ("delete", "destroy", "drop", "purge")):
            levels.append(RiskLevel.HIGH)
        if any(value in lowered for value in ("deploy", "push", "commit", "pull_request")):
            levels.append(RiskLevel.HIGH)
        return maximum_risk(*levels)


def _is_protected_path(raw_path: str, protected_targets: tuple[str, ...]) -> bool:
    normalized = posixpath.normpath(raw_path.replace("\\", "/")).strip("/")
    if not normalized or normalized == ".":
        return False
    parts = PurePosixPath(normalized).parts
    for protected in protected_targets:
        prot_norm = posixpath.normpath(protected.replace("\\", "/")).strip("/")
        prot_parts = PurePosixPath(prot_norm).parts
        if normalized == prot_norm or normalized.startswith(prot_norm + "/"):
            return True
        if len(prot_parts) == 1 and prot_parts[0] in parts:
            return True
        for i in range(len(parts) - len(prot_parts) + 1):
            if parts[i : i + len(prot_parts)] == prot_parts:
                return True
    return False


def _argument_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key, value in arguments.items():
        if key.casefold() in {"path", "paths", "target", "file", "directory"}:
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(item for item in value if isinstance(item, str))
    return tuple(paths)
