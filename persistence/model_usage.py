from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.dialects.postgresql import insert

from agents.base import AgentAttemptRecord
from persistence.database import Database
from persistence.tables import ModelCallRow, TaskAttemptRow, TaskRow


class UsageIntegrityError(RuntimeError):
    """Raised when model accounting cannot be tied to the declared agent attempt."""


class PostgresUsageRecorder:
    """Replay-safe model usage accounting bound to a task attempt and AgentSpec."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(self, attempt: AgentAttemptRecord) -> None:
        call_id = uuid5(
            NAMESPACE_URL,
            f"autoswe:model-call:{attempt.attempt_id}:{attempt.turn}",
        )
        async with self._database.transaction() as session:
            persisted_attempt = await session.get(TaskAttemptRow, attempt.attempt_id)
            if persisted_attempt is None:
                raise UsageIntegrityError(f"task attempt {attempt.attempt_id} does not exist")
            if persisted_attempt.task_id != attempt.task_id:
                raise UsageIntegrityError("model call task does not match its task attempt")
            if persisted_attempt.agent_spec_hash != attempt.agent_spec_hash:
                raise UsageIntegrityError(
                    "model call AgentSpec hash does not match its task attempt"
                )
            task = await session.get(TaskRow, attempt.task_id)
            if task is None or task.run_id != attempt.run_id:
                raise UsageIntegrityError("model call run does not match its task")
            values = {
                "id": call_id,
                "run_id": attempt.run_id,
                "task_id": attempt.task_id,
                "attempt_id": attempt.attempt_id,
                "trace_id": attempt.trace_id,
                "provider_request_id": attempt.provider_request_id,
                "model": attempt.model,
                "turn": attempt.turn,
                "agent_spec_hash": attempt.agent_spec_hash,
                "input_tokens": attempt.usage.input_tokens,
                "output_tokens": attempt.usage.output_tokens,
                "cached_input_tokens": attempt.usage.cached_input_tokens,
                "cost_usd": attempt.usage.cost_usd,
                "failure_class": (attempt.failure_class.value if attempt.failure_class else None),
                "validation_errors": list(attempt.validation_errors),
                "tool_call_ids": list(attempt.tool_call_ids),
            }
            statement = (
                insert(ModelCallRow)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_model_call_invocation_turn")
                .returning(ModelCallRow.id)
            )
            inserted = await session.scalar(statement)
            if inserted is None:
                existing = await session.get(ModelCallRow, call_id)
                if existing is None or not _same_call(existing, values):
                    raise UsageIntegrityError(
                        "replayed model turn conflicts with its durable accounting record"
                    )


def _same_call(row: ModelCallRow, values: dict[str, object]) -> bool:
    return all(getattr(row, field) == value for field, value in values.items())
