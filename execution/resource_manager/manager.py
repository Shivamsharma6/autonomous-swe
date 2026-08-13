from typing import Any, Dict


class ResourceManager:
    """Tracks concurrency, memory limits, and process worker allocation."""

    def __init__(self, max_concurrent_tasks: int = 5):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.active_tasks_count = 0

    def acquire_slot(self) -> bool:
        if self.active_tasks_count < self.max_concurrent_tasks:
            self.active_tasks_count += 1
            return True
        return False

    def release_slot(self) -> None:
        if self.active_tasks_count > 0:
            self.active_tasks_count -= 1
