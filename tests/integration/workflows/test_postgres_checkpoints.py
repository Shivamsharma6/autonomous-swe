from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langgraph.types import Command
from sqlalchemy import select

from domain.enums import GraphExecutionState, TaskStatus
from persistence.tables import GraphExecutionRow, TaskRow
from tests.integration.messaging.helpers import seed_task
from workflows.checkpoints import postgres_checkpointer
from workflows.runtime import CheckpointedWorkflowRuntime
from workflows.state import CheckpointIdentity, WaitKind, WaitWorkflowInput
from workflows.task_subgraphs import build_wait_graph


@pytest.mark.parametrize(
    ("wait_kind", "expected_state"),
    [
        (WaitKind.TOOL, GraphExecutionState.WAITING_FOR_TOOL),
        (WaitKind.APPROVAL, GraphExecutionState.WAITING_FOR_APPROVAL),
        (WaitKind.UAMS, GraphExecutionState.WAITING_FOR_MEMORY),
    ],
)
@pytest.mark.asyncio
async def test_postgres_checkpoint_survives_runtime_recreation_and_keeps_state_machines_separate(
    database: Any,
    postgres_urls: tuple[str, str],
    wait_kind: WaitKind,
    expected_state: GraphExecutionState,
) -> None:
    ids = await seed_task(database, run_state="RUNNING")
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
    identity = CheckpointIdentity(
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        project_id=ids["project_id"],
        repository_id=ids["repository_id"],
        baseline_commit="b" * 40,
    )
    workflow_input = WaitWorkflowInput(
        identity=identity,
        wait_kind=wait_kind,
        request_id=uuid4(),
    )

    async with postgres_checkpointer(postgres_urls[1], setup=True) as saver:
        graph = build_wait_graph(checkpointer=saver, production=True)
        runtime = CheckpointedWorkflowRuntime(database=database, graph=graph)
        paused = await runtime.invoke(identity, workflow_input.to_state())

    assert paused.state is expected_state
    assert paused.checkpoint_id
    assert paused.interrupt is not None
    assert paused.interrupt["kind"] == wait_kind.value

    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        graph_row = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
    assert task is not None and task.state is TaskStatus.RUNNING
    assert graph_row is not None and graph_row.state is expected_state
    assert graph_row.thread_id == identity.thread_id

    async with postgres_checkpointer(postgres_urls[1]) as saver:
        recreated_graph = build_wait_graph(checkpointer=saver, production=True)
        recreated_runtime = CheckpointedWorkflowRuntime(database=database, graph=recreated_graph)
        completed = await recreated_runtime.invoke(
            identity,
            Command(resume={"released": True, "request_id": str(workflow_input.request_id)}),
        )

    assert completed.state is GraphExecutionState.COMPLETED
    assert completed.values["resume_payload"]["released"] is True
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        graph_row = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
    assert task is not None and task.state is TaskStatus.RUNNING
    assert graph_row is not None and graph_row.state is GraphExecutionState.COMPLETED
