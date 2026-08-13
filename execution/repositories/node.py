from __future__ import annotations

import json
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

_LIFECYCLE_SCRIPTS = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "prepare",
        "prepublish",
        "prepublishOnly",
        "publish",
        "postpublish",
    }
)


class NodeRepositoryAdapter(RepositoryAdapter):
    name = "node"
    _lockfiles = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")

    def detect(self, root: Path) -> bool:
        return (root / "package.json").is_file()

    def inspect(self, root: Path) -> RepositoryManifest:
        root = canonical_root(root)
        package_path = root / "package.json"
        if not package_path.is_file():
            raise AdapterPolicyError("package.json is required for Node repositories")
        try:
            document = json.loads(read_bounded(package_path))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterPolicyError("package.json is not valid UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise AdapterPolicyError("package.json must contain an object")
        scripts = document.get("scripts", {})
        if scripts is not None and not isinstance(scripts, dict):
            raise AdapterPolicyError("package scripts must be an object")
        unsupported = sorted(_LIFECYCLE_SCRIPTS.intersection(scripts or {}))
        if unsupported:
            raise AdapterPolicyError(
                f"unsupported lifecycle script is not permitted: {', '.join(unsupported)}"
            )
        lockfile = select_lockfile(root, self._lockfiles)
        dependencies = _node_dependencies(document)
        return RepositoryManifest(
            adapter=self.name,
            root=root,
            lockfile=lockfile,
            source_files=discover_files(
                root,
                ("src", "app"),
                frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
            ),
            test_files=_actual_node_tests(
                discover_files(
                    root,
                    ("test", "tests", "src"),
                    frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
                )
            ),
            artifact_files=artifact_files(root, ("dist", "build")),
            dependencies=frozenset(dependencies),
            metadata_files=tuple(
                name
                for name in ("package.json", lockfile, "tsconfig.json")
                if (root / name).is_file()
            ),
        )

    def command(self, manifest: RepositoryManifest, request: CommandRequest) -> CommandSpec:
        if manifest.adapter != self.name:
            raise AdapterPolicyError("manifest was not produced by the Node adapter")
        if request.kind is CommandKind.INSTALL:
            return _node_install(manifest.lockfile)
        runner = _node_runner(manifest.lockfile)
        if request.kind is CommandKind.LINT:
            _require_dependency(manifest, "eslint")
            return CommandSpec(argv=(*runner, "eslint", "."), timeout_seconds=600)
        if request.kind is CommandKind.TYPECHECK:
            _require_dependency(manifest, "typescript")
            return CommandSpec(argv=(*runner, "tsc", "--noEmit"), timeout_seconds=900)
        if request.kind is CommandKind.TARGETED_TEST:
            framework = _test_framework(manifest)
            assert request.target is not None
            target = validate_target(
                manifest.root,
                request.target,
                allowed_files=_actual_node_tests(manifest.test_files),
            )
            return CommandSpec(
                argv=(*runner, framework, "run", target),
                timeout_seconds=900,
            )
        if request.kind is CommandKind.FULL_TEST:
            framework = _test_framework(manifest)
            return CommandSpec(argv=(*runner, framework, "run"), timeout_seconds=1_800)
        if request.kind is CommandKind.BUILD:
            _require_dependency(manifest, "typescript")
            if not (manifest.root / "tsconfig.json").is_file():
                raise UnsupportedCommandError("TypeScript build requires tsconfig.json")
            return CommandSpec(
                argv=(*runner, "tsc", "--project", "tsconfig.json"),
                timeout_seconds=1_200,
            )
        raise UnsupportedCommandError(f"unsupported Node command: {request.kind}")


def _node_dependencies(document: dict[str, Any]) -> set[str]:
    dependencies: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        group = document.get(field, {})
        if group is None:
            continue
        if not isinstance(group, dict) or not all(
            isinstance(name, str) and isinstance(version, str) for name, version in group.items()
        ):
            raise AdapterPolicyError(f"{field} must map package names to versions")
        dependencies.update(group)
    return dependencies


def _node_install(lockfile: str) -> CommandSpec:
    argv: tuple[str, ...]
    if lockfile in {"package-lock.json", "npm-shrinkwrap.json"}:
        argv = ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    elif lockfile == "pnpm-lock.yaml":
        argv = ("pnpm", "install", "--frozen-lockfile", "--ignore-scripts")
    else:
        argv = ("yarn", "install", "--immutable", "--mode=skip-builds")
    return CommandSpec(argv=argv, timeout_seconds=1_800, network_required=True)


def _node_runner(lockfile: str) -> tuple[str, ...]:
    if lockfile in {"package-lock.json", "npm-shrinkwrap.json"}:
        return ("npm", "exec", "--offline", "--")
    if lockfile == "pnpm-lock.yaml":
        return ("pnpm", "exec")
    return ("yarn", "exec")


def _require_dependency(manifest: RepositoryManifest, dependency: str) -> None:
    if dependency not in manifest.dependencies:
        raise UnsupportedCommandError(
            f"{dependency} must be declared in the immutable Node dependency set"
        )


def _test_framework(manifest: RepositoryManifest) -> str:
    if "vitest" in manifest.dependencies:
        return "vitest"
    if "jest" in manifest.dependencies:
        return "jest"
    raise UnsupportedCommandError("vitest or jest must be declared for test execution")


def _actual_node_tests(files: tuple[str, ...]) -> tuple[str, ...]:
    markers = (".test.", ".spec.")
    return tuple(
        path
        for path in files
        if path.startswith(("test/", "tests/")) or any(marker in path for marker in markers)
    )
