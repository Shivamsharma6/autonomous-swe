from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import SandboxRequest


class SandboxBoundaryViolation(RuntimeError):
    """A sandbox execution request asked for isolation the platform does not grant."""


class SandboxBoundarySettings(Protocol):
    """The configuration subset the boundary policy derives its rules from."""

    @property
    def repository_import_root(self) -> Path: ...

    @property
    def managed_worktree_root(self) -> Path: ...

    @property
    def python_runner_image(self) -> str: ...

    @property
    def node_runner_image(self) -> str: ...

    @property
    def host_uid(self) -> int: ...

    @property
    def host_gid(self) -> int: ...


def _contained(root: Path, candidate: Path) -> Path | None:
    """Return ``candidate`` when it resolves inside ``root``, else ``None``.

    Resolution happens first so a symlink that escapes the root is rejected
    rather than followed. ``relative_to`` is the containment idiom already used
    for repository onboarding in ``apps.api.routes``.
    """
    try:
        resolved_root = root.resolve(strict=False)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if resolved == resolved_root:
        return None
    return resolved


@dataclass(frozen=True, slots=True)
class SandboxBoundary:
    """Server-side containment for every sandbox execution request.

    The sandbox manager is the only component that can reach the Docker
    Engine, so it cannot treat isolation-relevant values as trusted input.
    ``SandboxRequest`` is a public HTTP body: it carries host mount paths, an
    image reference, and the container credentials. Each is re-derived here
    from the manager's own process configuration, so a compromised caller
    cannot bind the host root, mount an unvetted image, or run as a host
    account. Resource *limits* stay caller-declared because they can only make
    the sandbox more restrictive; identity and location cannot.
    """

    import_root: Path
    worktree_root: Path
    allowed_images: frozenset[str]
    uid: int
    gid: int

    @classmethod
    def from_settings(cls, settings: SandboxBoundarySettings) -> SandboxBoundary:
        return cls(
            import_root=settings.repository_import_root,
            worktree_root=settings.managed_worktree_root,
            allowed_images=frozenset({settings.python_runner_image, settings.node_runner_image}),
            uid=settings.host_uid,
            gid=settings.host_gid,
        )

    def enforce(self, request: SandboxRequest) -> SandboxRequest:
        source = _contained(self.import_root, request.source_repository)
        if source is None:
            raise SandboxBoundaryViolation(
                "source repository must be a directory inside the configured import root"
            )
        worktree = _contained(self.worktree_root, request.worktree)
        if worktree is None:
            raise SandboxBoundaryViolation(
                "mutable worktree must be a directory inside the configured managed root"
            )
        if request.policy.image not in self.allowed_images:
            raise SandboxBoundaryViolation("sandbox image is not in the configured allowlist")
        if request.policy.uid != self.uid or request.policy.gid != self.gid:
            raise SandboxBoundaryViolation(
                "sandbox container credentials must match the configured host identity"
            )
        if request.environment:
            # Defence in depth: the runner also allowlists names, but a caller
            # that reached this point has already been authenticated with a
            # different trust level than the platform configuration.
            raise SandboxBoundaryViolation("sandbox environment overrides are not accepted")
        policy = SandboxPolicy(
            **{
                **request.policy.model_dump(),
                "image": request.policy.image,
                "uid": self.uid,
                "gid": self.gid,
            }
        )
        return request.model_copy(
            update={"source_repository": source, "worktree": worktree, "policy": policy}
        )

    def assert_no_escape(self, paths: Iterable[Path]) -> None:
        for path in paths:
            if _contained(self.worktree_root, path) is None:
                raise SandboxBoundaryViolation(f"path {path} escapes the managed worktree root")
