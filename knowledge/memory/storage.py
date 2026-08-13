"""Compatibility imports for the external UAMS memory port.

SQLite storage has been removed. This module intentionally exposes no local storage
implementation and will be deleted after all legacy callers migrate to PostgreSQL,
artifact storage, and ``MemoryPort``.
"""

from knowledge.memory.port import MemoryPort
from knowledge.memory.uams import UAMSMemoryAdapter

__all__ = ["MemoryPort", "UAMSMemoryAdapter"]
