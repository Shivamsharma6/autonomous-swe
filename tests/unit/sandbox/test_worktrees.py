from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from execution.sandbox.worktrees import GitWorktreeManager, WorktreePolicyError

GIT_EXECUTABLE = shutil.which("git")


def git(*arguments: str, cwd: Path) -> str:
    if GIT_EXECUTABLE is None:
        pytest.skip("Git executable is unavailable")
    completed = subprocess.run(  # noqa: S603 - test arguments are local literals and UUIDs
        (GIT_EXECUTABLE, *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git("init", "-b", "main", cwd=root)
    git("config", "user.email", "tests@example.invalid", cwd=root)
    git("config", "user.name", "Tests", cwd=root)
    (root / "app.py").write_text("VALUE = 1\n")
    git("add", "app.py", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    return root


def test_mutable_tasks_get_deterministic_separate_branches_and_worktrees(
    source_repository: Path, tmp_path: Path
) -> None:
    managed_root = tmp_path / "managed"
    manager = GitWorktreeManager(managed_root)
    first_task = uuid4()
    second_task = uuid4()
    baseline = git("rev-parse", "HEAD", cwd=source_repository)

    first = manager.create_task_worktree(source_repository, first_task, baseline)
    replay = manager.create_task_worktree(source_repository, first_task, baseline)
    second = manager.create_task_worktree(source_repository, second_task, baseline)

    assert first == replay
    assert first != second
    assert git("branch", "--show-current", cwd=first) == f"autoswe/task/{first_task}"
    assert git("branch", "--show-current", cwd=second) == f"autoswe/task/{second_task}"
    assert first.is_relative_to(managed_root.resolve())
    assert source_repository not in first.parents


def test_integration_worktree_is_separate_from_task_worktrees(
    source_repository: Path, tmp_path: Path
) -> None:
    manager = GitWorktreeManager(tmp_path / "managed")
    baseline = git("rev-parse", "HEAD", cwd=source_repository)
    task = manager.create_task_worktree(source_repository, uuid4(), baseline)
    run_id = uuid4()
    integration = manager.create_integration_worktree(source_repository, run_id, baseline)

    assert integration != task
    assert git("branch", "--show-current", cwd=integration) == f"autoswe/integration/{run_id}"


def test_dependent_worktree_integrates_modified_and_untracked_files_idempotently(
    source_repository: Path, tmp_path: Path
) -> None:
    manager = GitWorktreeManager(tmp_path / "managed")
    baseline = git("rev-parse", "HEAD", cwd=source_repository)
    dependency_id = uuid4()
    target_id = uuid4()
    dependency = manager.create_task_worktree(source_repository, dependency_id, baseline)
    target = manager.create_task_worktree(source_repository, target_id, baseline)
    (dependency / "app.py").write_text("VALUE = 2\n")
    (dependency / "new.py").write_text("NEW = True\n")

    manager.integrate_task_dependencies(source_repository, target, (dependency_id,))
    manager.integrate_task_dependencies(source_repository, target, (dependency_id,))

    assert (target / "app.py").read_text() == "VALUE = 2\n"
    assert (target / "new.py").read_text() == "NEW = True\n"


def test_worktree_source_and_baseline_are_strictly_validated(
    source_repository: Path, tmp_path: Path
) -> None:
    manager = GitWorktreeManager(tmp_path / "managed")

    with pytest.raises(WorktreePolicyError, match="commit"):
        manager.create_task_worktree(source_repository, uuid4(), "main; touch owned")
    with pytest.raises(WorktreePolicyError, match="Git repository"):
        manager.create_task_worktree(tmp_path, uuid4(), "a" * 40)


def test_symlinked_managed_path_cannot_escape(source_repository: Path, tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    task_id = uuid4()
    (managed_root / f"task-{task_id}").symlink_to(outside, target_is_directory=True)
    manager = GitWorktreeManager(managed_root)
    baseline = git("rev-parse", "HEAD", cwd=source_repository)

    with pytest.raises(WorktreePolicyError, match="symlink"):
        manager.create_task_worktree(source_repository, task_id, baseline)


def test_cleanup_requires_terminal_state_and_is_idempotent(
    source_repository: Path, tmp_path: Path
) -> None:
    manager = GitWorktreeManager(tmp_path / "managed")
    task_id = uuid4()
    baseline = git("rev-parse", "HEAD", cwd=source_repository)
    worktree = manager.create_task_worktree(source_repository, task_id, baseline)

    with pytest.raises(WorktreePolicyError, match="terminal"):
        manager.cleanup(source_repository, worktree, terminal=False)
    assert manager.cleanup(source_repository, worktree, terminal=True) is True
    assert manager.cleanup(source_repository, worktree, terminal=True) is False
    assert not worktree.exists()
