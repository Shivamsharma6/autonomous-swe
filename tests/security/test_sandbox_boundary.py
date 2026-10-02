"""The sandbox manager is the only component that can reach the Docker Engine.

These tests pin the boundary it must apply to values that arrive on a public
HTTP body: host mount paths, the image reference, and the container user. A
caller holding the service credential is not trusted to choose where a
container binds or who it runs as.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from execution.repositories import CommandSpec
from execution.sandbox.boundary import SandboxBoundary, SandboxBoundaryViolation
from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import SandboxRequest

_DIGEST = hashlib.sha256(b"autoswe-test-image").hexdigest()
PYTHON_IMAGE = f"registry.local/autoswe/python-runner@sha256:{_DIGEST}"
NODE_IMAGE = f"registry.local/autoswe/node-runner@sha256:{_DIGEST}"


@dataclass(frozen=True)
class FakeSettings:
    repository_import_root: Path
    managed_worktree_root: Path
    python_runner_image: str = PYTHON_IMAGE
    node_runner_image: str = NODE_IMAGE
    host_uid: int = 65532
    host_gid: int = 65532


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    imports = tmp_path / "imports"
    worktrees = tmp_path / "worktrees"
    (imports / "acme").mkdir(parents=True)
    (worktrees / "task-1").mkdir(parents=True)
    return imports, worktrees, tmp_path


@pytest.fixture
def boundary(roots: tuple[Path, Path, Path]) -> SandboxBoundary:
    imports, worktrees, _ = roots
    return SandboxBoundary.from_settings(
        FakeSettings(repository_import_root=imports, managed_worktree_root=worktrees)
    )


def policy_for(image: str = PYTHON_IMAGE, *, uid: int = 65532, gid: int = 65532) -> SandboxPolicy:
    return SandboxPolicy(
        image=image,
        uid=uid,
        gid=gid,
        cpu_nanos=1_000_000_000,
        cpu_time_limit_ms=60_000,
        memory_bytes=256 * 1024 * 1024,
        pids_limit=64,
        timeout_seconds=60,
        max_stdout_bytes=8_192,
        max_stderr_bytes=8_192,
        max_total_output_bytes=16_384,
    )


def request_for(
    roots: tuple[Path, Path, Path],
    *,
    source: Path | None = None,
    worktree: Path | None = None,
    **kwargs,
) -> SandboxRequest:
    from uuid import uuid4

    imports, worktrees, _ = roots
    return SandboxRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        source_repository=source if source is not None else imports / "acme",
        worktree=worktree if worktree is not None else worktrees / "task-1",
        command=CommandSpec(argv=("python", "-c", "print(1)"), timeout_seconds=30),
        policy=policy_for(**kwargs),
    )


def test_permitted_request_is_returned_with_normalised_paths(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    governed = boundary.enforce(request_for(roots))
    imports, worktrees, _ = roots
    assert governed.source_repository == (imports / "acme").resolve()
    assert governed.worktree == (worktrees / "task-1").resolve()
    assert governed.policy.uid == boundary.uid


@pytest.mark.parametrize("escape", ["host_root", "parent_of_root", "sibling_root"])
def test_worktree_outside_the_managed_root_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path], tmp_path: Path, escape: str
) -> None:
    _, worktrees, _ = roots
    if escape == "host_root":
        target = tmp_path
    elif escape == "parent_of_root":
        target = worktrees.parent
    else:
        target = worktrees.parent / "elsewhere"
        target.mkdir()
    with pytest.raises(SandboxBoundaryViolation, match="managed root"):
        boundary.enforce(request_for(roots, worktree=target))


def test_host_root_mount_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    # The exact regression: "/" is an existing directory, is not a symlink, and
    # therefore satisfies every check the request model itself performs.
    with pytest.raises(SandboxBoundaryViolation, match="import root"):
        boundary.enforce(request_for(roots, source=Path("/")))


def test_symlinked_intermediate_component_escaping_the_root_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, worktrees, _ = roots
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    # The leaf is a real directory, so the request model's own leaf-symlink
    # check passes; only resolution against the root reveals the escape.
    (worktrees / "task-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxBoundaryViolation, match="managed root"):
        boundary.enforce(request_for(roots, worktree=worktrees / "task-link" / "nested"))


def test_leaf_symlink_is_rejected_by_the_request_contract(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, worktrees, _ = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktrees / "task-escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be symlinks"):
        request_for(roots, worktree=worktrees / "task-escape")


def test_the_root_itself_is_not_a_valid_mount(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    _, worktrees, _ = roots
    with pytest.raises(SandboxBoundaryViolation):
        boundary.enforce(request_for(roots, worktree=worktrees))


def test_unlisted_image_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    other = f"registry.local/attacker/root-shell@sha256:{'0' * 64}"
    with pytest.raises(SandboxBoundaryViolation, match="allowlist"):
        boundary.enforce(request_for(roots, image=other))


def test_the_other_allowed_image_is_accepted(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    assert boundary.enforce(request_for(roots, image=NODE_IMAGE)).policy.image == NODE_IMAGE


@pytest.mark.parametrize("uid", [0, 1000, 65533])
def test_caller_chosen_container_user_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path], uid: int
) -> None:
    if uid == 0:
        pytest.skip("uid 0 is already rejected by the request contract")
    with pytest.raises(SandboxBoundaryViolation, match="host identity"):
        boundary.enforce(request_for(roots, uid=uid))


def test_caller_chosen_container_group_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    with pytest.raises(SandboxBoundaryViolation, match="host identity"):
        boundary.enforce(request_for(roots, gid=20))


def test_caller_supplied_environment_is_rejected(
    boundary: SandboxBoundary, roots: tuple[Path, Path, Path]
) -> None:
    permitted = request_for(roots).model_copy(update={"environment": {"CI": "true"}})
    with pytest.raises(SandboxBoundaryViolation, match="environment"):
        boundary.enforce(permitted)
