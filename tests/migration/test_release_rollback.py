from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from langgraph.types import Command
from sqlalchemy import create_engine, inspect, text

from domain.enums import GraphExecutionState, TaskStatus
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import TaskRow
from tests.integration.messaging.helpers import seed_task
from workflows.checkpoints import postgres_checkpointer
from workflows.runtime import CheckpointedWorkflowRuntime
from workflows.state import CheckpointIdentity, WaitKind, WaitWorkflowInput
from workflows.task_subgraphs import build_wait_graph


def _alembic(sync_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_url)
    return config


@pytest.mark.asyncio
async def test_prior_schema_checkpoint_resumes_across_upgrade_and_reversible_rollback(
    postgres_urls: tuple[str, str],
) -> None:
    async_url, sync_url = postgres_urls
    sync_engine = create_engine(sync_url)
    with sync_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = _alembic(sync_url)
    command.upgrade(config, "0009")
    assert "workflow_node_executions" not in inspect(sync_engine).get_table_names()

    database = Database(async_url)
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
    request_id = uuid4()
    prior_input = WaitWorkflowInput(
        identity=identity,
        wait_kind=WaitKind.APPROVAL,
        request_id=request_id,
    )
    async with postgres_checkpointer(sync_url, setup=True) as saver:
        prior_graph = build_wait_graph(checkpointer=saver, production=True)
        # Seed the prior release's checkpoint directly. Running today's ORM
        # against 0009 would reference columns introduced by later migrations.
        config_values = {"configurable": {"thread_id": identity.thread_id}}
        paused = await prior_graph.ainvoke(
            prior_input.to_state(),
            config_values,
            durability="sync",
        )
        snapshot = await prior_graph.aget_state(config_values)
    assert paused["__interrupt__"]
    async with database.transaction() as session:
        await DomainRepository().record_graph_execution(
            session,
            task_id=identity.task_id,
            run_id=identity.run_id,
            repository_id=identity.repository_id,
            baseline_commit=identity.baseline_commit,
            thread_id=identity.thread_id,
            state=GraphExecutionState.WAITING_FOR_APPROVAL,
            checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
        )
    await database.dispose()

    command.upgrade(config, "head")
    assert "workflow_node_executions" in inspect(sync_engine).get_table_names()

    upgraded_database = Database(async_url)
    async with postgres_checkpointer(sync_url) as saver:
        upgraded_graph = build_wait_graph(checkpointer=saver, production=True)
        upgraded_runtime = CheckpointedWorkflowRuntime(
            database=upgraded_database,
            graph=cast(Any, upgraded_graph),
        )
        completed = await upgraded_runtime.invoke(
            identity,
            Command(resume={"released": True, "request_id": str(request_id)}),
        )
    assert completed.state is GraphExecutionState.COMPLETED
    await upgraded_database.dispose()

    command.downgrade(config, "0009")
    tables_after_rollback = set(inspect(sync_engine).get_table_names())
    assert "workflow_node_executions" not in tables_after_rollback
    assert {"projects", "tasks", "checkpoints", "checkpoint_writes"}.issubset(tables_after_rollback)
    with sync_engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM tasks WHERE id = :task_id"),
                {"task_id": ids["task_id"]},
            )
            == 1
        )
        assert connection.scalar(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
            {"thread_id": identity.thread_id},
        )

    command.upgrade(config, "head")
    command.check(config)
    sync_engine.dispose()


def test_backup_restore_scripts_verify_hash_catalog_and_pre_restore_snapshot() -> None:
    backup = Path("scripts/backup.sh").read_text()
    restore = Path("scripts/restore.sh").read_text()

    assert "pg_restore --list" in backup
    assert "shasum -a 256" in backup
    assert "pg_restore --list" in restore
    assert "pre-restore-" in restore
    assert "--exit-on-error" in restore
    assert "AUTOSWE_SKIP_PRE_RESTORE_BACKUP" in restore
