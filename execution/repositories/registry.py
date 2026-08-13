from __future__ import annotations

from pathlib import Path

from execution.repositories.base import AdapterPolicyError, RepositoryAdapter, canonical_root
from execution.repositories.node import NodeRepositoryAdapter
from execution.repositories.python import PythonRepositoryAdapter


class RepositoryAdapterRegistry:
    def __init__(self, adapters: tuple[RepositoryAdapter, ...]) -> None:
        if not adapters:
            raise ValueError("at least one repository adapter is required")
        names = tuple(adapter.name for adapter in adapters)
        if len(names) != len(set(names)):
            raise ValueError("repository adapter names must be unique")
        self._adapters = adapters

    @classmethod
    def default(cls) -> RepositoryAdapterRegistry:
        return cls((PythonRepositoryAdapter(), NodeRepositoryAdapter()))

    def detect(self, root: Path) -> RepositoryAdapter:
        root = canonical_root(root)
        matches = tuple(adapter for adapter in self._adapters if adapter.detect(root))
        if not matches:
            raise AdapterPolicyError("no supported repository manifest was detected")
        if len(matches) > 1:
            names = ", ".join(sorted(adapter.name for adapter in matches))
            raise AdapterPolicyError(f"multiple repository adapters matched: {names}")
        return matches[0]

