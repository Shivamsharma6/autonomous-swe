from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from domain.enums import TaskType
from workflows.feature import build_admitted_task_graph
from workflows.state import (
    AdmittedTask,
    SchedulerDispatchBatch,
    TaskDispatchResult,
)


class ConcurrentRunner:
    def __init__(self) -> None:
        self.running = 0
        self.max_observed = 0
        self.executed: list[UUID] = []
        self._lock = asyncio.Lock()

    async def execute(self, task: AdmittedTask) -> TaskDispatchResult:
        async with self._lock:
            self.running += 1
            self.max_observed = max(self.max_observed, self.running)
            self.executed.append(task.task_id)
        await asyncio.sleep(0.02 if task.task_id.int % 2 else 0.005)
        async with self._lock:
            self.running -= 1
        return TaskDispatchResult(
            task_id=task.task_id,
            result_id=uuid4(),
            artifact_ids=(uuid4(),),
            message_ids=(uuid4(),),
        )


@pytest.mark.asyncio
async def test_fanout_executes_only_scheduler_admitted_tasks_and_fanin_is_deterministic() -> None:
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    independent = tuple(
        AdmittedTask(
            task_id=uuid4(),
            task_type=task_type,
            dependencies=(),
        )
        for task_type in (
            TaskType.RESEARCH,
            TaskType.IMPLEMENTATION,
            TaskType.TEST,
        )
    )
    integration = AdmittedTask(
        task_id=uuid4(),
        task_type=TaskType.VALIDATION,
        dependencies=tuple(task.task_id for task in independent),
    )
    runner = ConcurrentRunner()
    graph = build_admitted_task_graph(runner)
    first_batch = SchedulerDispatchBatch(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="a" * 40,
        admitted_tasks=independent,
        completed_task_ids=(),
        scheduler_parallel_limit=3,
    )

    state = await graph.ainvoke(first_batch.to_state())

    assert set(runner.executed) == {task.task_id for task in independent}
    assert integration.task_id not in runner.executed
    assert runner.max_observed == 3
    assert state["ordered_task_ids"] == sorted(str(task.task_id) for task in independent)

    second_batch = SchedulerDispatchBatch(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="a" * 40,
        admitted_tasks=(integration,),
        completed_task_ids=tuple(task.task_id for task in independent),
        scheduler_parallel_limit=3,
    )
    await graph.ainvoke(second_batch.to_state())

    assert runner.executed.count(integration.task_id) == 1
