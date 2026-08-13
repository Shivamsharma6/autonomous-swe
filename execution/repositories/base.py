from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from domain.models import ContractModel

_SHELL_SYNTAX = re.compile(r"[;&|`$<>\n\r\x00]")
_MAX_DISCOVERED_FILES = 20_000


class AdapterPolicyError(ValueError):
    """A repository or requested operation violates adapter policy."""


class AmbiguousLockfileError(AdapterPolicyError):
    """More than one dependency authority was found."""


class UnsupportedCommandError(AdapterPolicyError):
    """The repository does not declare the governed tool needed by a command."""


class CommandKind(StrEnum):
    INSTALL = "install"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TARGETED_TEST = "targeted_test"
    FULL_TEST = "full_test"
    BUILD = "build"


class CommandRequest(ContractModel):
    kind: CommandKind
    target: Annotated[str, Field(min_length=1, max_length=1_024)] | None = None

    @model_validator(mode="after")
    def target_matches_kind(self) -> CommandRequest:
        if self.kind is CommandKind.TARGETED_TEST and self.target is None:
            raise ValueError("targeted_test requires a target")
        if self.kind is not CommandKind.TARGETED_TEST and self.target is not None:
            raise ValueError("target is only valid for targeted_test")
        return self


class CommandSpec(ContractModel):
    argv: tuple[Annotated[str, Field(min_length=1, max_length=4_096)], ...] = Field(
        min_length=1,
        max_length=100,
    )
    cwd: str = "."
    timeout_seconds: int = Field(ge=1, le=7_200)
    network_required: bool = False

    @model_validator(mode="after")
    def is_an_argument_array(self) -> CommandSpec:
        if any(
            "\x00" in argument or "\n" in argument or "\r" in argument
            for argument in self.argv
        ):
            raise ValueError("command arguments cannot contain control characters")
        if self.cwd != ".":
            raise ValueError("repository commands must execute at the managed root")
        return self


class RepositoryManifest(ContractModel):
    adapter: str
    root: Path
    lockfile: str
    source_files: tuple[str, ...]
    test_files: tuple[str, ...]
    artifact_files: tuple[str, ...]
    dependencies: frozenset[str] = frozenset()
    metadata_files: tuple[str, ...] = ()

    @model_validator(mode="after")
    def root_is_canonical(self) -> RepositoryManifest:
        if not self.root.is_absolute() or self.root != self.root.resolve():
            raise ValueError("repository root must be absolute and canonical")
        return self


class RepositoryAdapter(ABC):
    name: str

    @abstractmethod
    def detect(self, root: Path) -> bool:
        """Return whether the repository has this adapter's authoritative manifest."""

    @abstractmethod
    def inspect(self, root: Path) -> RepositoryManifest:
        """Read and validate the bounded repository manifest."""

    @abstractmethod
    def command(self, manifest: RepositoryManifest, request: CommandRequest) -> CommandSpec:
        """Select a governed argv array from repository metadata and adapter policy."""

    def collect_artifacts(self, manifest: RepositoryManifest) -> tuple[str, ...]:
        return manifest.artifact_files


def canonical_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise AdapterPolicyError(f"repository root is unavailable: {root}") from exc
    if not resolved.is_dir():
        raise AdapterPolicyError(f"repository root is not a directory: {root}")
    return resolved


def read_bounded(path: Path, *, maximum_bytes: int = 1_048_576) -> bytes:
    if path.is_symlink():
        raise AdapterPolicyError(f"manifest cannot be a symlink: {path.name}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AdapterPolicyError(f"cannot inspect manifest: {path.name}") from exc
    if size > maximum_bytes:
        raise AdapterPolicyError(f"manifest exceeds {maximum_bytes} bytes: {path.name}")
    return path.read_bytes()


def select_lockfile(root: Path, candidates: tuple[str, ...]) -> str:
    found = tuple(name for name in candidates if (root / name).is_file())
    if not found:
        raise AdapterPolicyError("a supported immutable lockfile is required")
    if len(found) != 1:
        raise AmbiguousLockfileError(
            f"multiple supported lockfiles make dependency resolution ambiguous: {', '.join(found)}"
        )
    if (root / found[0]).is_symlink():
        raise AdapterPolicyError("lockfile cannot be a symlink")
    return found[0]


def validate_target(
    root: Path,
    raw_path: str,
    *,
    allowed_files: tuple[str, ...],
) -> str:
    if (
        not raw_path
        or Path(raw_path).is_absolute()
        or "\\" in raw_path
        or ":" in raw_path
        or _SHELL_SYNTAX.search(raw_path)
        or ".." in Path(raw_path).parts
    ):
        raise AdapterPolicyError("target must be a safe repository-relative test path")
    candidate = root / raw_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AdapterPolicyError("target must remain inside the managed repository") from exc
    if candidate.is_symlink() or not resolved.is_file() or raw_path not in allowed_files:
        raise AdapterPolicyError("target must identify a discovered test file")
    return raw_path


def discover_files(
    root: Path,
    directories: tuple[str, ...],
    suffixes: frozenset[str],
    *,
    reject_symlinks: bool = False,
) -> tuple[str, ...]:
    discovered: list[str] = []
    for directory in directories:
        base = root / directory
        if not base.exists():
            continue
        if base.is_symlink():
            if reject_symlinks:
                raise AdapterPolicyError(f"symlink is not allowed in {directory}")
            continue
        for candidate in base.rglob("*"):
            if candidate.is_symlink():
                if reject_symlinks:
                    raise AdapterPolicyError(f"symlink is not allowed in {directory}")
                continue
            if not candidate.is_file() or candidate.suffix not in suffixes:
                continue
            try:
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise AdapterPolicyError(
                    f"discovered file escapes repository: {candidate}"
                ) from exc
            discovered.append(candidate.relative_to(root).as_posix())
            if len(discovered) > _MAX_DISCOVERED_FILES:
                raise AdapterPolicyError("repository discovery file limit exceeded")
    return tuple(sorted(set(discovered)))


def artifact_files(root: Path, directories: tuple[str, ...]) -> tuple[str, ...]:
    return discover_files(
        root,
        directories,
        frozenset(_artifact_suffixes()),
        reject_symlinks=True,
    )


def _artifact_suffixes() -> tuple[str, ...]:
    return (".whl", ".gz", ".zip", ".js", ".mjs", ".cjs", ".map", ".ts")
