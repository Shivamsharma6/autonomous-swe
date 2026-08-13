from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from execution.repositories import CommandSpec
from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import SandboxRequest

PINNED_IMAGE = "registry.example/runner@sha256:" + "b" * 64


def policy() -> SandboxPolicy:
    return SandboxPolicy(
        image=PINNED_IMAGE,
        uid=65532,
        gid=65532,
        cpu_nanos=500_000_000,
        cpu_time_limit_ms=30_000,
        memory_bytes=256 * 1024 * 1024,
        pids_limit=64,
        timeout_seconds=120,
        max_stdout_bytes=1_000_000,
        max_stderr_bytes=1_000_000,
        max_total_output_bytes=2_000_000,
    )


def test_source_and_worktree_must_be_distinct_canonical_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    command = CommandSpec(argv=("python", "-V"), timeout_seconds=10)

    with pytest.raises(ValidationError, match="distinct"):
        SandboxRequest(
            execution_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            attempt_id=uuid4(),
            source_repository=source,
            worktree=source,
            command=command,
            policy=policy(),
        )


def test_source_and_worktree_symlinks_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    real_worktree = tmp_path / "real-worktree"
    real_worktree.mkdir()
    linked_worktree = tmp_path / "linked-worktree"
    linked_worktree.symlink_to(real_worktree, target_is_directory=True)

    with pytest.raises(ValidationError, match="symlink"):
        SandboxRequest(
            execution_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            attempt_id=uuid4(),
            source_repository=source,
            worktree=linked_worktree,
            command=CommandSpec(argv=("python", "-V"), timeout_seconds=10),
            policy=policy(),
        )


def test_environment_is_allowlisted_and_secrets_are_forbidden(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(ValidationError, match="environment"):
        SandboxRequest(
            execution_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            attempt_id=uuid4(),
            source_repository=source,
            worktree=worktree,
            command=CommandSpec(argv=("python", "-V"), timeout_seconds=10),
            policy=policy(),
            environment={"OPENAI_API_KEY": "secret"},
        )


def test_command_cannot_select_a_host_working_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cwd"):
        CommandSpec(
            argv=("python", "-V"),
            cwd=str(tmp_path),
            timeout_seconds=10,
        )
