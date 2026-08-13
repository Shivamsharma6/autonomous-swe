from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from agents.base import AgentInvocation, AgentRuntime
from agents.scripted import ScriptedGateway, ScriptedResponse
from persistence.model_usage import PostgresUsageRecorder, UsageIntegrityError
from persistence.tables import ModelCallRow, TaskAttemptRow
from tests.integration.messaging.helpers import seed_task
from tests.unit.agents.test_runtime import VerifiedResult, response, spec


@pytest.mark.asyncio
async def test_every_model_attempt_persists_usage_and_exact_agent_spec_hash(
    database: Any,
) -> None:
    ids = await seed_task(database)
    agent_spec = spec()
    async with database.transaction() as session:
        attempt = await session.get(TaskAttemptRow, ids["attempt_id"])
        assert attempt is not None
        attempt.agent_spec_hash = agent_spec.spec_hash
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(response({"schema_version": "1.0", "answer": ""})),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "verified"})),
        )
    )
    recorder = PostgresUsageRecorder(database)
    runtime = AgentRuntime(
        agent_spec,
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        usage_recorder=recorder,
        max_schema_repairs=1,
    )
    invocation = AgentInvocation(
        trace_id="trace-persisted-usage",
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        project_id=ids["project_id"],
        repository_id=ids["repository_id"],
        baseline_commit="b" * 40,
        goal="Validate durable model accounting.",
        input_payload={},
    )

    result = await runtime.run(invocation)
    await recorder.record(result.attempts[-1])

    assert result.output.answer == "verified"
    async with database.transaction() as session:
        rows = tuple(
            (await session.scalars(select(ModelCallRow).order_by(ModelCallRow.turn))).all()
        )
    assert len(rows) == 2
    assert [row.turn for row in rows] == [1, 2]
    assert rows[0].validation_errors
    assert rows[1].validation_errors == []
    assert all(row.agent_spec_hash == agent_spec.spec_hash for row in rows)
    assert sum(row.input_tokens + row.output_tokens for row in rows) == 30

    conflicting = result.attempts[-1].model_copy(update={"agent_spec_hash": "f" * 64})
    with pytest.raises(UsageIntegrityError, match="AgentSpec hash"):
        await recorder.record(conflicting)
