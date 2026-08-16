from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agents.base import (
    AgentAttemptRecord,
    AgentInvocation,
    AgentRuntime,
    ToolDispatcher,
    UsageRecorder,
)
from agents.gateway import ModelGateway, ToolCall, ToolDefinition
from domain.enums import RiskLevel, TaskStatus
from domain.messages import ContextHandoff
from domain.models import AgentSpec, ContractModel, ToolCallRequest
from knowledge.memory.port import MemoryPort
from persistence.artifacts import ArtifactService
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import ModelCallRow, TaskRow, WorkflowNodeExecutionRow, utc_now
from tools.approval import ApprovalService
from tools.gateway import ToolCallResult, ToolExecutionStatus, ToolGateway
from tools.production import ProductionToolSet
from tools.registry import ToolExecutionContext, ToolRegistry
from workflows.state import NodeExecutionRequest, NodeExecutionResult


class NodeAgentOutput(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    summary: str = Field(min_length=1, max_length=20_000)
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=200)
    changed_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=1_000)
    verification_passed: bool | None = None


_NODE_ROLES = {
    "recall": "researcher",
    "investigate": "researcher",
    "evidence": "validation",
    "synthesis": "researcher",
    "implement": "coder",
    "targeted_test": "tester",
    "review": "reviewer",
    "generate_tests": "tester",
    "execute": "tester",
    "establish_invariants": "validation",
    "refactor": "coder",
    "regression_verify": "tester",
    "draft": "documentation",
    "validate_examples": "validation",
    "inspect": "validation",
    "verify": "validation",
}

_NODE_TOOL_POLICY = {
    "recall": (),
    "investigate": ("read_file", "search_code"),
    "evidence": ("read_file", "search_code"),
    "synthesis": ("read_file", "search_code"),
    "implement": ("read_file", "search_code", "apply_patch", "run_tests"),
    "targeted_test": ("read_file", "search_code", "run_tests"),
    "review": ("read_file", "search_code", "run_tests"),
    "generate_tests": ("read_file", "search_code", "apply_patch"),
    "execute": ("run_tests",),
    "establish_invariants": ("read_file", "search_code", "run_tests"),
    "refactor": ("read_file", "search_code", "apply_patch", "run_tests"),
    "regression_verify": ("run_tests",),
    "draft": ("read_file", "search_code", "apply_patch"),
    "validate_examples": ("read_file", "run_tests"),
    "inspect": ("read_file", "search_code"),
    "verify": ("read_file", "search_code", "run_tests"),
}


class PostgresUsageRecorder(UsageRecorder):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(self, attempt: AgentAttemptRecord) -> None:
        record_id = uuid5(
            NAMESPACE_URL,
            f"model-call:{attempt.attempt_id}:{attempt.trace_id}:{attempt.turn}",
        )
        async with self._database.transaction() as session:
            await session.execute(
                insert(ModelCallRow)
                .values(
                    id=record_id,
                    run_id=attempt.run_id,
                    task_id=attempt.task_id,
                    attempt_id=attempt.attempt_id,
                    trace_id=attempt.trace_id,
                    provider_request_id=attempt.provider_request_id,
                    model=attempt.model,
                    turn=attempt.turn,
                    agent_spec_hash=attempt.agent_spec_hash,
                    input_tokens=attempt.usage.input_tokens,
                    output_tokens=attempt.usage.output_tokens,
                    cached_input_tokens=attempt.usage.cached_input_tokens,
                    cost_usd=attempt.usage.cost_usd,
                    failure_class=attempt.failure_class.value if attempt.failure_class else None,
                    validation_errors=list(attempt.validation_errors),
                    tool_call_ids=list(attempt.tool_call_ids),
                    created_at=utc_now(),
                )
                .on_conflict_do_nothing(constraint="uq_model_call_invocation_turn")
            )


class GatewayToolDispatcher(ToolDispatcher):
    def __init__(
        self,
        *,
        gateway: ToolGateway,
        registry: ToolRegistry,
        project_id: UUID,
        repository_id: UUID,
        baseline_commit: str,
        worktree: Path,
        risk_ceiling: RiskLevel,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._project_id = project_id
        self._repository_id = repository_id
        self._baseline_commit = baseline_commit
        self._worktree = worktree
        self._risk_ceiling = risk_ceiling
        self.results: list[ToolCallResult] = []

    async def dispatch(self, call: ToolCall, *, invocation: AgentInvocation) -> dict[str, Any]:
        registered = self._registry.resolve(call.name, "1.0")
        request = ToolCallRequest(
            call_id=uuid5(
                NAMESPACE_URL,
                f"agent-tool:{invocation.attempt_id}:{invocation.trace_id}:{call.call_id}",
            ),
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            attempt_id=invocation.attempt_id,
            requested_by=self._role(invocation),
            tool_name=call.name,
            arguments=call.arguments,
            idempotency_key=(
                f"agent-tool:{invocation.attempt_id}:{invocation.trace_id}:{call.call_id}"
            ),
        )
        result = await self._gateway.execute(
            request,
            context=ToolExecutionContext(
                project_id=self._project_id,
                repository_id=self._repository_id,
                run_id=invocation.run_id,
                task_id=invocation.task_id,
                attempt_id=invocation.attempt_id,
                baseline_commit=self._baseline_commit,
                agent_role=self._role(invocation),
                agent_capabilities=frozenset({registered.spec.owning_capability}),
                risk_ceiling=self._risk_ceiling,
                worktree_root=self._worktree,
            ),
        )
        self.results.append(result)
        return result.model_dump(mode="json")

    @staticmethod
    def _role(invocation: AgentInvocation) -> str:
        role = invocation.input_payload.get("agent_role")
        if not isinstance(role, str) or not role:
            raise ValueError("agent invocation is missing its bound role")
        return role


class ProductionNodeExecutor:
    def __init__(
        self,
        *,
        database: Database,
        memory: MemoryPort,
        model_gateway: ModelGateway,
        tool_set: ProductionToolSet,
        project_id: UUID,
        repository_id: UUID,
        baseline_commit: str,
        allowed_tools: tuple[str, ...],
        risk_ceiling: RiskLevel,
        primary_model: str,
        fallback_models: tuple[str, ...],
        artifacts: ArtifactService,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._memory = memory
        self._model_gateway = model_gateway
        self._tool_set = tool_set
        self._registry = tool_set.registry()
        self._tool_gateway = ToolGateway(
            database=database,
            registry=self._registry,
            approvals=ApprovalService(database=database),
        )
        self._project_id = project_id
        self._repository_id = repository_id
        self._baseline_commit = baseline_commit
        self._allowed_tools = frozenset(allowed_tools)
        self._risk_ceiling = risk_ceiling
        self._primary_model = primary_model
        self._fallback_models = fallback_models
        self._artifacts = artifacts
        self._repository = repository or DomainRepository()
        self._usage = PostgresUsageRecorder(database)

    async def cancellation_requested(self, task_id: UUID) -> bool:
        async with self._database.sessions() as session:
            task = await session.get(TaskRow, task_id)
            return task is None or task.state is TaskStatus.CANCELLED

    async def execute(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        replay = await self._claim(request)
        if replay is not None:
            return replay
        try:
            output, memory_ids, tool_calls, role = await self._invoke(request)
            return await self._persist(
                request,
                output=output,
                memory_ids=memory_ids,
                tool_calls=tool_calls,
                role=role,
            )
        except Exception:
            await self._mark_failed(request)
            raise

    async def _claim(self, request: NodeExecutionRequest) -> NodeExecutionResult | None:
        execution_id = request.idempotency_uuid("node-execution")
        async with self._database.transaction() as session:
            await session.execute(
                insert(WorkflowNodeExecutionRow)
                .values(
                    id=execution_id,
                    task_id=request.task_id,
                    attempt_id=request.attempt_id,
                    node_name=request.node_name,
                    idempotency_key=request.idempotency_key,
                    status="RUNNING",
                    result={},
                    started_at=utc_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=[WorkflowNodeExecutionRow.idempotency_key]
                )
            )
            row = await session.scalar(
                select(WorkflowNodeExecutionRow)
                .where(WorkflowNodeExecutionRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if row is None or row.id != execution_id or row.attempt_id != request.attempt_id:
                raise PermissionError("workflow node idempotency identity conflict")
            if row.status == "COMPLETED":
                return NodeExecutionResult.model_validate(row.result)
            row.status = "RUNNING"
            row.result = {}
            row.completed_at = None
            return None

    async def _invoke(
        self, request: NodeExecutionRequest
    ) -> tuple[NodeAgentOutput, tuple[UUID, ...], tuple[ToolCall, ...], str]:
        role = _NODE_ROLES[request.node_name]
        tool_names = tuple(
            name
            for name in _NODE_TOOL_POLICY[request.node_name]
            if name in self._allowed_tools
        )
        definitions = tuple(self._tool_definition(name) for name in tool_names)
        spec = AgentSpec(
            role=role,
            purpose=f"Execute the {request.node_name} stage for a {request.task_type.value} task.",
            input_schema="AgentInvocation@1.0",
            output_schema="NodeAgentOutput@1.0",
            primary_model=self._primary_model,
            fallback_models=self._fallback_models,
            tool_grants=tool_names,
            maximum_risk=self._risk_ceiling,
            memory_policy="verified-external-uams-context",
            token_budget=20_000,
            cost_budget_usd=2.0,
            turn_budget=12,
            wall_time_seconds=900,
            sandbox_profile="task-isolated",
            network_profile="none",
            retry_policy="transient-provider-fallback-and-one-schema-repair",
            escalation_policy="human-on-policy-or-uncertain-side-effect",
            termination_policy="valid-structured-output-or-visible-failure",
        )
        dispatcher = GatewayToolDispatcher(
            gateway=self._tool_gateway,
            registry=self._registry,
            project_id=self._project_id,
            repository_id=self._repository_id,
            baseline_commit=self._baseline_commit,
            worktree=self._tool_set.worktree,
            risk_ceiling=self._risk_ceiling,
        )
        runtime = AgentRuntime(
            spec,
            self._model_gateway,
            input_type=AgentInvocation,
            output_type=NodeAgentOutput,
            memory=self._memory,
            tool_definitions=definitions,
            tool_dispatcher=dispatcher,
            usage_recorder=self._usage,
        )
        result = await runtime.run(
            AgentInvocation(
                trace_id=f"{request.trace_id}:node:{request.node_name}",
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                project_id=request.project_id,
                repository_id=request.repository_id,
                baseline_commit=request.baseline_commit,
                goal=request.goal,
                input_payload={
                    "agent_role": role,
                    "task_type": request.task_type.value,
                    "node": request.node_name,
                    "input_refs": request.input_refs,
                    "prior_summaries": request.prior_summaries,
                    "prior_message_ids": [str(value) for value in request.prior_message_ids],
                    "prior_artifact_ids": [str(value) for value in request.prior_artifact_ids],
                },
            )
        )
        validate_tool_evidence(request.node_name, result.output, tuple(dispatcher.results))
        return result.output, result.context_memory_ids, result.tool_calls, role

    async def _persist(
        self,
        request: NodeExecutionRequest,
        *,
        output: NodeAgentOutput,
        memory_ids: tuple[UUID, ...],
        tool_calls: tuple[ToolCall, ...],
        role: str,
    ) -> NodeExecutionResult:
        artifact_id = request.idempotency_uuid("artifact")
        message_id = request.idempotency_uuid("message")
        result = NodeExecutionResult(
            message_ids=(message_id,),
            artifact_ids=(artifact_id,),
            result_id=request.idempotency_uuid("result"),
            summary=output.summary,
        )
        content = json.dumps(
            {
                "schema_version": "1.0",
                "node": request.node_name,
                "role": role,
                "output": output.model_dump(mode="json"),
                "memory_ids": [str(value) for value in memory_ids],
                "tool_calls": [call.model_dump(mode="json") for call in tool_calls],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        async with self._database.transaction() as session:
            node = await session.scalar(
                select(WorkflowNodeExecutionRow)
                .where(WorkflowNodeExecutionRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if node is None:
                raise RuntimeError("workflow node execution claim disappeared")
            if node.status == "COMPLETED":
                return NodeExecutionResult.model_validate(node.result)
            await self._artifacts.put(
                session,
                content=content,
                media_type="application/vnd.autoswe.node-result+json",
                project_id=request.project_id,
                run_id=request.run_id,
                task_id=request.task_id,
                artifact_id=artifact_id,
            )
            await self._repository.persist_message(
                session,
                ContextHandoff(
                    message_id=message_id,
                    sender=role,
                    recipient="workflow",
                    run_id=request.run_id,
                    task_id=request.task_id,
                    attempt_id=request.attempt_id,
                    created_at=node.started_at,
                    causation_id=request.idempotency_uuid("causation"),
                    correlation_id=request.run_id,
                    artifact_ids=(artifact_id,),
                    summary=output.summary,
                    context_ids=memory_ids,
                ),
            )
            node.status = "COMPLETED"
            node.result = result.model_dump(mode="json")
            node.completed_at = utc_now()
        return result

    async def _mark_failed(self, request: NodeExecutionRequest) -> None:
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(WorkflowNodeExecutionRow)
                .where(WorkflowNodeExecutionRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if row is not None and row.status != "COMPLETED":
                row.status = "FAILED"
                row.completed_at = utc_now()

    def _tool_definition(self, name: str) -> ToolDefinition:
        registered = self._registry.resolve(name, "1.0")
        return ToolDefinition(
            name=name,
            description=f"Governed AutoSWE {name} operation in the isolated task worktree.",
            input_schema=registered.argument_schema,
        )


def validate_tool_evidence(
    node_name: str,
    output: NodeAgentOutput,
    results: tuple[ToolCallResult, ...],
) -> None:
    """Refuse model claims that are not supported by the durable tool outcomes."""
    latest_by_tool: dict[str, ToolCallResult] = {}
    for result in results:
        latest_by_tool[result.tool_name] = result
    mutation_nodes = {"implement", "generate_tests", "refactor", "draft"}
    patch_result = latest_by_tool.get("apply_patch")
    if node_name in mutation_nodes and patch_result is not None:
        if patch_result.status is not ToolExecutionStatus.COMPLETED:
            raise RuntimeError("mutation node requires a successful apply_patch result")
    elif node_name in mutation_nodes and output.changed_paths:
        raise RuntimeError("changed paths require a successful apply_patch result")

    unrecovered = tuple(
        name
        for name, result in latest_by_tool.items()
        if result.status is not ToolExecutionStatus.COMPLETED
    )
    if unrecovered:
        raise RuntimeError(
            "agent returned final output after an unrecovered tool failure: "
            + ", ".join(sorted(unrecovered))
        )

    test_result = latest_by_tool.get("run_tests")
    if output.verification_passed is not None and test_result is not None:
        passed = test_result.output.get("passed")
        if not isinstance(passed, bool) or passed is not output.verification_passed:
            raise RuntimeError(
                "agent verification claim does not match the governed run_tests result"
            )
