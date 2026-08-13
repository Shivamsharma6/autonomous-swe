from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from execution.repositories.base import (
    AdapterPolicyError,
    CommandKind,
    CommandRequest,
    CommandSpec,
    RepositoryAdapter,
    RepositoryManifest,
    UnsupportedCommandError,
    artifact_files,
    canonical_root,
    discover_files,
    read_bounded,
    select_lockfile,
    validate_target,
)


class PythonRepositoryAdapter(RepositoryAdapter):
    name = "python"
    _lockfiles = ("uv.lock", "poetry.lock", "requirements.lock", "requirements.txt")

    def detect(self, root: Path) -> bool:
        return (root / "pyproject.toml").is_file()

    def inspect(self, root: Path) -> RepositoryManifest:
        root = canonical_root(root)
        manifest_path = root / "pyproject.toml"
        if not manifest_path.is_file():
            raise AdapterPolicyError("pyproject.toml is required for Python repositories")
        try:
            document = tomllib.loads(read_bounded(manifest_path).decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise AdapterPolicyError("pyproject.toml is not valid UTF-8 TOML") from exc
        if not isinstance(document.get("project"), dict):
            raise AdapterPolicyError("pyproject.toml must contain a project table")
        lockfile = select_lockfile(root, self._lockfiles)
        dependencies = _python_dependencies(document)
        return RepositoryManifest(
            adapter=self.name,
            root=root,
            lockfile=lockfile,
            source_files=discover_files(root, ("src",), frozenset({".py", ".pyi"})),
            test_files=discover_files(root, ("tests", "test"), frozenset({".py", ".pyi"})),
            artifact_files=artifact_files(root, ("dist",)),
            dependencies=frozenset(dependencies),
            metadata_files=("pyproject.toml", lockfile),
        )

    def command(self, manifest: RepositoryManifest, request: CommandRequest) -> CommandSpec:
        if manifest.adapter != self.name:
            raise AdapterPolicyError("manifest was not produced by the Python adapter")
        prefix = _runner_prefix(manifest.lockfile)
        if request.kind is CommandKind.INSTALL:
            return _python_install(manifest.lockfile)
        if request.kind is CommandKind.LINT:
            _require_dependency(manifest, "ruff")
            return CommandSpec(argv=(*prefix, "ruff", "check", "."), timeout_seconds=600)
        if request.kind is CommandKind.TYPECHECK:
            _require_dependency(manifest, "mypy")
            targets = tuple(
                directory
                for directory in ("src", "tests", "test")
                if (manifest.root / directory).is_dir()
            )
            return CommandSpec(argv=(*prefix, "mypy", *targets), timeout_seconds=900)
        if request.kind is CommandKind.TARGETED_TEST:
            assert request.target is not None
            target = validate_target(
                manifest.root,
                request.target,
                allowed_files=manifest.test_files,
            )
            if "pytest" not in manifest.dependencies:
                module = target.removesuffix(".py").replace("/", ".")
                return CommandSpec(
                    argv=("python", "-m", "unittest", module),
                    timeout_seconds=900,
                )
            return CommandSpec(argv=(*prefix, "pytest", target), timeout_seconds=900)
        if request.kind is CommandKind.FULL_TEST:
            if "pytest" not in manifest.dependencies:
                test_root = "tests" if (manifest.root / "tests").is_dir() else "test"
                return CommandSpec(
                    argv=("python", "-m", "unittest", "discover", "-s", test_root),
                    timeout_seconds=1_800,
                )
            return CommandSpec(argv=(*prefix, "pytest"), timeout_seconds=1_800)
        if request.kind is CommandKind.BUILD:
            if manifest.lockfile == "uv.lock":
                return CommandSpec(argv=("uv", "build"), timeout_seconds=1_200)
            if manifest.lockfile == "poetry.lock":
                return CommandSpec(argv=("poetry", "build"), timeout_seconds=1_200)
            _require_dependency(manifest, "build")
            return CommandSpec(argv=(*prefix, "python", "-m", "build"), timeout_seconds=1_200)
        raise UnsupportedCommandError(f"unsupported Python command: {request.kind}")


def _python_dependencies(document: dict[str, Any]) -> set[str]:
    raw: list[str] = []
    project = document.get("project", {})
    if isinstance(project, dict):
        raw.extend(item for item in project.get("dependencies", []) if isinstance(item, str))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    raw.extend(item for item in group if isinstance(item, str))
    groups = document.get("dependency-groups", {})
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                raw.extend(item for item in group if isinstance(item, str))
    tool = document.get("tool", {})
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies", {})
            if isinstance(poetry_dependencies, dict):
                raw.extend(str(name) for name in poetry_dependencies)
    names: set[str] = set()
    for requirement in raw:
        name = requirement.split(";", 1)[0]
        for separator in ("[", "<", ">", "=", "!", "~", " "):
            name = name.split(separator, 1)[0]
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


def _runner_prefix(lockfile: str) -> tuple[str, ...]:
    if lockfile == "uv.lock":
        return ("uv", "run")
    if lockfile == "poetry.lock":
        return ("poetry", "run")
    return ()


def _python_install(lockfile: str) -> CommandSpec:
    if lockfile == "uv.lock":
        return CommandSpec(
            argv=("uv", "sync", "--frozen"),
            timeout_seconds=1_800,
            network_required=True,
        )
    if lockfile == "poetry.lock":
        return CommandSpec(
            argv=("poetry", "install", "--no-interaction", "--sync"),
            timeout_seconds=1_800,
            network_required=True,
        )
    return CommandSpec(
        argv=("python", "-m", "pip", "install", "--require-hashes", "-r", lockfile),
        timeout_seconds=1_800,
        network_required=True,
    )


def _require_dependency(manifest: RepositoryManifest, dependency: str) -> None:
    if dependency not in manifest.dependencies:
        raise UnsupportedCommandError(
            f"{dependency} must be declared in the immutable Python dependency set"
        )
