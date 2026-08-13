from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_GIT_EXECUTABLE = shutil.which("git")


class WorktreePolicyError(RuntimeError):
    """A requested Git worktree operation is outside the managed boundary."""


class GitWorktreeManager:
    def __init__(self, managed_root: Path) -> None:
        managed_root.mkdir(parents=True, exist_ok=True)
        if managed_root.is_symlink():
            raise WorktreePolicyError("managed worktree root cannot be a symlink")
        self._managed_root = managed_root.resolve(strict=True)

    def create_task_worktree(
        self,
        source_repository: Path,
        task_id: UUID,
        baseline_commit: str,
    ) -> Path:
        return self._create(
            source_repository,
            self._managed_root / f"task-{task_id}",
            f"autoswe/task/{task_id}",
            baseline_commit,
        )

    def create_integration_worktree(
        self,
        source_repository: Path,
        run_id: UUID,
        baseline_commit: str,
    ) -> Path:
        return self._create(
            source_repository,
            self._managed_root / f"integration-{run_id}",
            f"autoswe/integration/{run_id}",
            baseline_commit,
        )

    def cleanup(
        self,
        source_repository: Path,
        worktree: Path,
        *,
        terminal: bool,
    ) -> bool:
        if not terminal:
            raise WorktreePolicyError("worktree cleanup requires a terminal task or run")
        source = self._source(source_repository)
        candidate = self._managed_child(worktree)
        if not candidate.exists() and not candidate.is_symlink():
            self._git(source, "worktree", "prune")
            return False
        if candidate.is_symlink():
            raise WorktreePolicyError("managed worktree cannot be a symlink")
        self._git(source, "worktree", "remove", "--force", str(candidate))
        self._git(source, "worktree", "prune")
        return True

    def _create(
        self,
        source_repository: Path,
        destination: Path,
        branch: str,
        baseline_commit: str,
    ) -> Path:
        source = self._source(source_repository)
        if not _COMMIT.fullmatch(baseline_commit):
            raise WorktreePolicyError("baseline commit must be an immutable Git object ID")
        self._git(source, "cat-file", "-e", f"{baseline_commit}^{{commit}}")
        destination = self._managed_child(destination)
        if destination.is_symlink():
            raise WorktreePolicyError("managed worktree destination cannot be a symlink")
        if destination.exists():
            current_branch = self._git(destination, "branch", "--show-current")
            if current_branch != branch:
                raise WorktreePolicyError("existing worktree belongs to another branch")
            return destination.resolve(strict=True)
        branch_exists = self._git_optional(
            source,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        )
        if branch_exists:
            self._git(source, "worktree", "add", str(destination), branch)
        else:
            self._git(
                source,
                "worktree",
                "add",
                "-b",
                branch,
                str(destination),
                baseline_commit,
            )
        return destination.resolve(strict=True)

    def _source(self, source_repository: Path) -> Path:
        if source_repository.is_symlink():
            raise WorktreePolicyError("source Git repository cannot be a symlink")
        try:
            source = source_repository.resolve(strict=True)
        except OSError as exc:
            raise WorktreePolicyError("source Git repository is unavailable") from exc
        if not source.is_dir():
            raise WorktreePolicyError("source Git repository must be a directory")
        try:
            self._git(source, "rev-parse", "--git-dir")
        except WorktreePolicyError as exc:
            raise WorktreePolicyError("source must be a Git repository") from exc
        return source

    def _managed_child(self, path: Path) -> Path:
        if path.parent.resolve(strict=True) != self._managed_root:
            raise WorktreePolicyError("worktree must be a direct child of the managed root")
        if not path.name.startswith(("task-", "integration-")):
            raise WorktreePolicyError("worktree name is not managed")
        return path

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        if _GIT_EXECUTABLE is None:
            raise WorktreePolicyError("Git executable is unavailable")
        try:
            completed = subprocess.run(  # noqa: S603 - all values are policy-generated
                (_GIT_EXECUTABLE, "-C", str(repository), *arguments),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorktreePolicyError(f"governed Git operation failed: {detail}") from exc
        return completed.stdout.strip()

    @staticmethod
    def _git_optional(repository: Path, *arguments: str) -> bool:
        if _GIT_EXECUTABLE is None:
            raise WorktreePolicyError("Git executable is unavailable")
        try:
            completed = subprocess.run(  # noqa: S603 - all values are policy-generated
                (_GIT_EXECUTABLE, "-C", str(repository), *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorktreePolicyError("cannot inspect governed Git branch") from exc
        return completed.returncode == 0
