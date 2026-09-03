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

    @property
    def managed_root(self) -> Path:
        return self._managed_root

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

    def integrate_task_dependencies(
        self,
        source_repository: Path,
        target_worktree: Path,
        dependency_ids: tuple[UUID, ...],
    ) -> None:
        """Replay dependency worktree changes into a dependent task exactly once."""
        source = self._source(source_repository)
        target = self._managed_child(target_worktree)
        if not target.is_dir() or target.is_symlink():
            raise WorktreePolicyError("target task worktree is unavailable")
        for dependency_id in sorted(dependency_ids, key=lambda value: value.int):
            dependency = self._managed_child(
                self._managed_root / f"task-{dependency_id}"
            )
            if not dependency.is_dir() or dependency.is_symlink():
                raise WorktreePolicyError(
                    f"completed dependency worktree is unavailable: {dependency_id}"
                )
            diff_files = [
                line.strip()
                for line in self._git(dependency, "diff", "--name-only", "HEAD").splitlines()
                if line.strip()
            ]
            for rel in diff_files:
                src_path = dependency / rel
                dst_path = target / rel
                if src_path.is_file():
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dst_path)
            self._copy_untracked(dependency, target)
        self._git(source, "worktree", "list", "--porcelain")

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

    def commit_task_worktree(self, worktree: Path, *, message: str) -> str:
        """Perform the exact local commit only after the caller proves approval."""
        candidate = self._managed_child(worktree)
        if not candidate.is_dir() or candidate.is_symlink():
            raise WorktreePolicyError("approved task worktree is unavailable")
        normalized = " ".join(message.split())
        if not normalized or len(normalized) > 300:
            raise WorktreePolicyError("commit message must be a bounded single line")
        self._git(candidate, "add", "--all")
        staged = self._git_optional(candidate, "diff", "--cached", "--quiet")
        if not staged:
            self._git(
                candidate,
                "-c",
                "user.name=AutoSWE",
                "-c",
                "user.email=autoswe@localhost.invalid",
                "commit",
                "--no-gpg-sign",
                "-m",
                normalized,
            )
        return self._git(candidate, "rev-parse", "HEAD")

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

    @staticmethod
    def _git_with_input(repository: Path, value: str, *arguments: str) -> str:
        if _GIT_EXECUTABLE is None:
            raise WorktreePolicyError("Git executable is unavailable")
        try:
            completed = subprocess.run(  # noqa: S603 - fixed Git and policy arguments
                (_GIT_EXECUTABLE, "-C", str(repository), *arguments),
                input=value,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorktreePolicyError(f"dependency integration failed: {detail}") from exc
        return completed.stdout.strip()

    @staticmethod
    def _git_with_input_optional(repository: Path, value: str, *arguments: str) -> bool:
        if _GIT_EXECUTABLE is None:
            raise WorktreePolicyError("Git executable is unavailable")
        try:
            completed = subprocess.run(  # noqa: S603 - fixed Git and policy arguments
                (_GIT_EXECUTABLE, "-C", str(repository), *arguments),
                input=value,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorktreePolicyError("cannot inspect dependency integration") from exc
        return completed.returncode == 0

    def _copy_untracked(self, dependency: Path, target: Path) -> None:
        output = self._git(dependency, "ls-files", "--others", "--exclude-standard", "-z")
        for raw in output.split("\x00"):
            if not raw:
                continue
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise WorktreePolicyError("untracked dependency path escapes worktree")
            source = (dependency / relative).resolve(strict=True)
            destination = (target / relative).resolve(strict=False)
            source.relative_to(dependency)
            destination.relative_to(target)
            if source.is_symlink() or not source.is_file():
                raise WorktreePolicyError("untracked dependency content must be a regular file")
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
                    raise WorktreePolicyError(
                        f"dependency integration conflict at {relative.as_posix()}"
                    )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
