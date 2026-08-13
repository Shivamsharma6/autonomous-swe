import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from policies.risk.policy_engine import RiskLevel


class TaskStatus(str, Enum):
    """Status of a task/workflow node."""

    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskNode(BaseModel):
    """A node in the workflow DAG."""

    id: str
    title: str = ""
    name: str = ""
    description: str = ""
    assigned_agent: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    tool_calls: List[Any] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if not self.title and self.name:
            self.title = self.name
        elif not self.name and self.title:
            self.name = self.title


class TaskPlanner:
    """Generates task DAGs for software engineering workflows."""

    def generate_dag(self, goal: str) -> List[TaskNode]:
        """Generates structured DAG nodes for an SDLC workflow."""
        t1 = TaskNode(
            id="dag-1",
            title="Research Repository Context",
            name="Research Repository Context",
            assigned_agent="Researcher",
            status=TaskStatus.PENDING,
        )
        t2 = TaskNode(
            id="dag-2",
            title="Implement Source Code Feature",
            name="Implement Source Code Feature",
            assigned_agent="Coder",
            dependencies=["dag-1"],
            status=TaskStatus.PENDING,
        )
        t3 = TaskNode(
            id="dag-3",
            title="Generate Unit Tests & Mocks",
            name="Generate Unit Tests & Mocks",
            assigned_agent="Tester",
            dependencies=["dag-2"],
            status=TaskStatus.PENDING,
        )
        t4 = TaskNode(
            id="dag-4",
            title="Review Code Quality & Run Tests",
            name="Review Code Quality & Run Tests",
            assigned_agent="Reviewer",
            dependencies=["dag-3"],
            status=TaskStatus.PENDING,
        )
        return [t1, t2, t3, t4]


class TaskScheduler:
    """Schedules and manages execution leases for task DAGs."""

    def __init__(self, lease_ttl_sec: float = 30.0):
        self.tasks: Dict[str, TaskNode] = {}
        self.leases: Dict[str, Dict[str, Any]] = {}
        self.lease_ttl_sec = lease_ttl_sec
        self._lock = threading.Lock()

    def register_task(self, node: TaskNode) -> None:
        with self._lock:
            if not node.dependencies:
                node.status = TaskStatus.READY
            else:
                deps_met = all(
                    dep_id in self.tasks and self.tasks[dep_id].status == TaskStatus.COMPLETED
                    for dep_id in node.dependencies
                )
                node.status = TaskStatus.READY if deps_met else TaskStatus.PENDING
            self.tasks[node.id] = node

    def get_ready_tasks(self) -> List[TaskNode]:
        with self._lock:
            ready = []
            for task_id, task in self.tasks.items():
                if task.status == TaskStatus.PENDING:
                    deps_met = all(
                        dep_id in self.tasks and self.tasks[dep_id].status == TaskStatus.COMPLETED
                        for dep_id in task.dependencies
                    )
                    if deps_met:
                        task.status = TaskStatus.READY
                if task.status == TaskStatus.READY:
                    ready.append(task)
            return ready

    def lease_task(self, task_id: str, worker_id: str) -> Optional[TaskNode]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status != TaskStatus.READY:
                return None
            task.status = TaskStatus.LEASED
            self.leases[task_id] = {
                "worker_id": worker_id,
                "last_heartbeat": time.time(),
            }
            return task

    def send_heartbeat(self, task_id: str, worker_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status not in (TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.IN_PROGRESS):
                return False
            lease = self.leases.get(task_id)
            if lease and lease["worker_id"] == worker_id:
                lease["last_heartbeat"] = time.time()
                return True
            return False

    def reclaim_expired_leases(self) -> None:
        with self._lock:
            now = time.time()
            for task_id, lease in list(self.leases.items()):
                if now - lease["last_heartbeat"] > self.lease_ttl_sec:
                    task = self.tasks.get(task_id)
                    if task and task.status in (TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.IN_PROGRESS):
                        task.status = TaskStatus.READY
                    self.leases.pop(task_id, None)

    def complete_task(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if task:
                task.status = TaskStatus.COMPLETED
                self.leases.pop(task_id, None)

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if task:
                task.status = TaskStatus.CANCELLED
                self.leases.pop(task_id, None)

    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            task = self.tasks.get(task_id)
            return task.status if task else None
