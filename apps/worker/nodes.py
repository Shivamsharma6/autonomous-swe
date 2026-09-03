from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agents.base import (
    AgentAttemptRecord,
    AgentBudgetExceeded,
    AgentInvocation,
    AgentRuntime,
    StructuredOutputExhausted,
    ToolDispatcher,
    UsageRecorder,
)
from agents.gateway import GatewayError, ModelGateway, ToolCall, ToolDefinition
from agents.specs import AgentRole, build_agent_specs
from domain.enums import RiskLevel, TaskStatus
from domain.messages import ContextHandoff
from domain.models import AgentSpec, ContractModel, ToolCallRequest
from domain.task_policy import capabilities_for_assignment
from knowledge.memory.port import MemoryPort
from persistence.artifacts import ArtifactService
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    ModelCallRow,
    TaskRow,
    ToolExecutionRow,
    WorkflowNodeExecutionRow,
    utc_now,
)
from planning.service import default_repository_context
from policies.guardrails.secret_redactor import SecretRedactor
from tools.approval import ApprovalService
from tools.gateway import (
    ApprovalRequired,
    ToolCallResult,
    ToolExecutionStatus,
    ToolGateway,
    _result_from_row,
)
from tools.production import ProductionToolSet
from tools.registry import ToolExecutionContext, ToolRegistry
from workflows.state import NodeExecutionRequest, NodeExecutionResult


def _fingerprint_worktree(root: Path, changed_paths: tuple[str, ...]) -> dict[str, str]:
    resolved_root = root.resolve()
    fingerprint: dict[str, str] = {}
    for relative in changed_paths[:1_000]:
        candidate = (resolved_root / relative).resolve()
        if resolved_root not in candidate.parents and candidate != resolved_root:
            continue
        if not candidate.is_file():
            fingerprint[relative] = ""
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        fingerprint[relative] = digest
    return fingerprint


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
    "evidence": (),
    "synthesis": (),
    "implement": ("read_file", "search_code", "apply_patch", "run_tests"),
    "targeted_test": ("read_file", "search_code", "run_tests"),
    "review": ("read_file", "search_code", "run_tests"),
    "generate_tests": ("read_file", "search_code", "apply_patch", "run_tests"),
    "execute": ("read_file", "search_code", "run_tests"),
    "establish_invariants": ("read_file", "search_code", "run_tests"),
    "refactor": ("read_file", "search_code", "apply_patch", "run_tests"),
    "regression_verify": ("read_file", "search_code", "run_tests"),
    "draft": ("read_file", "search_code", "apply_patch", "run_tests"),
    "validate_examples": ("read_file", "search_code", "run_tests"),
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
        agent_capabilities: frozenset[str],
        risk_ceiling: RiskLevel,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._project_id = project_id
        self._repository_id = repository_id
        self._baseline_commit = baseline_commit
        self._worktree = worktree
        self._agent_capabilities = agent_capabilities
        self._risk_ceiling = risk_ceiling
        self.results: list[ToolCallResult] = []

    async def dispatch(self, call: ToolCall, *, invocation: AgentInvocation) -> dict[str, Any]:
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
        # Prevent repetitive duplicate test executions when tests have already succeeded.
        if call.name == "run_tests":
            last_patch_index = -1
            for idx, res in enumerate(self.results):
                if res.tool_name == "apply_patch" and res.status is ToolExecutionStatus.COMPLETED:
                    last_patch_index = idx
            for idx, res in enumerate(self.results):
                if (
                    idx > last_patch_index
                    and res.tool_name == "run_tests"
                    and res.status is ToolExecutionStatus.COMPLETED
                    and isinstance(res.output, dict)
                    and res.output.get("passed") is True
                ):
                    result = ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        tool_version=request.tool_version,
                        status=ToolExecutionStatus.COMPLETED,
                        output={
                            "passed": True,
                            "note": (
                                "Tests already passed successfully since the last code change. "
                                "Verification is complete. Stop calling tools and immediately return "
                                "the final NodeAgentOutput JSON."
                            ),
                        },
                        error=None,
                        risk=self._risk_ceiling,
                        attempts=0,
                    )
                    self.results.append(result)
                    return result.model_dump(mode="json")

        # Prevent repetitive duplicate read_file calls on unchanged files.
        if call.name == "read_file":
            req_path = str(call.arguments.get("path", "")).strip()
            last_patch_index = -1
            for idx, res in enumerate(self.results):
                if (
                    res.tool_name == "apply_patch"
                    and res.status is ToolExecutionStatus.COMPLETED
                    and str(res.output.get("path", "")).strip() == req_path
                ):
                    last_patch_index = idx
            for idx, res in enumerate(self.results):
                if (
                    idx > last_patch_index
                    and res.tool_name == "read_file"
                    and res.status is ToolExecutionStatus.COMPLETED
                    and isinstance(res.output, dict)
                    and str(res.output.get("path", "")).strip() == req_path
                ):
                    directive = (
                        "You MUST now call 'apply_patch' to apply your code changes. Do not call read_file."
                        if "apply_patch" in self._agent_capabilities
                        else "Stop calling tools and immediately return the final NodeAgentOutput JSON."
                    )
                    result = ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        tool_version=request.tool_version,
                        status=ToolExecutionStatus.COMPLETED,
                        output={
                            "path": req_path,
                            "error": f"File '{req_path}' has already been read into context. {directive}",
                        },
                        error=None,
                        risk=self._risk_ceiling,
                        attempts=0,
                    )
                    self.results.append(result)
                    return result.model_dump(mode="json")
        try:
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
                    # Declared task capability from the persisted TaskSpec, never
                    # derived from the tool being authorized.
                    agent_capabilities=self._agent_capabilities,
                    risk_ceiling=self._risk_ceiling,
                    worktree_root=self._worktree,
                ),
            )
        except ApprovalRequired:
            # Approval-gated tools are consequential by definition; an agent
            # loop cannot pause for a human, so the call is denied visibly
            # instead of crashing the node into a retry loop.
            result = ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                tool_version=request.tool_version,
                status=ToolExecutionStatus.FAILED,
                output={},
                error=(
                    "tool requires exact-call human approval and is not "
                    "executable inside an agent loop"
                ),
                risk=self._risk_ceiling,
                attempts=0,
            )
        except (ValueError, PermissionError, LookupError) as error:
            # These are pre-execution contract/policy denials, not uncertain
            # side effects. Return feedback without weakening the gateway.
            result = ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                tool_version=request.tool_version,
                status=ToolExecutionStatus.FAILED,
                output={},
                error=str(SecretRedactor().redact(str(error)))[:2_000],
                risk=self._risk_ceiling,
                attempts=0,
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
        assigned_capability: str,
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
        if not assigned_capability.strip():
            raise ValueError("task assigned capability must be declared")
        self._assigned_capability = assigned_capability
        self._risk_ceiling = risk_ceiling
        self._primary_model = primary_model
        self._fallback_models = fallback_models
        self._agent_specs = build_agent_specs(
            primary_model=primary_model, fallback_models=fallback_models
        )
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
        except Exception as error:
            await self._mark_failed(request, error)
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
                .on_conflict_do_nothing(index_elements=[WorkflowNodeExecutionRow.idempotency_key])
            )
            row = await session.scalar(
                select(WorkflowNodeExecutionRow)
                .where(WorkflowNodeExecutionRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if row is None or row.id != execution_id or row.attempt_id != request.attempt_id:
                raise PermissionError("workflow node idempotency identity conflict")
            if row.status == "COMPLETED":
                replay = NodeExecutionResult.model_validate(row.result)
                if await self._fingerprint_matches(replay.worktree_fingerprint):
                    return replay
                # The recorded side effects no longer exist in the current
                # worktree (it was recreated from baseline); the stored success
                # is not honest anymore, so re-execute.
            row.status = "RUNNING"
            row.result = {}
            row.completed_at = None
            return None

    async def _worktree_fingerprint(self, changed_paths: tuple[str, ...]) -> dict[str, str]:
        if not changed_paths:
            return {}
        return await asyncio.to_thread(
            _fingerprint_worktree, self._tool_set.worktree, tuple(changed_paths)
        )

    async def _fingerprint_matches(self, recorded: dict[str, str]) -> bool:
        if not recorded:
            return True
        current = await self._worktree_fingerprint(tuple(recorded))
        return current == recorded

    async def _invoke(
        self, request: NodeExecutionRequest
    ) -> tuple[NodeAgentOutput, tuple[UUID, ...], tuple[ToolCall, ...], str]:
        role = _NODE_ROLES[request.node_name]
        tool_names = tuple(
            name for name in _NODE_TOOL_POLICY[request.node_name] if name in self._allowed_tools
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
            # Count repeated prompt tokens while allowing a normal multi-file
            # tool exchange to finish within the same bound as other agents.
            token_budget=self._agent_specs[AgentRole(role)].token_budget,
            cost_budget_usd=2.0,
            turn_budget=self._agent_specs[AgentRole(role)].turn_budget,
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
            agent_capabilities=capabilities_for_assignment(self._assigned_capability),
            risk_ceiling=self._risk_ceiling,
        )
        prior_results = await self._prior_tool_results(request)

        def require_evidence(output: NodeAgentOutput) -> None:
            try:
                validated_output = output
                if output.changed_paths:
                    actual_writes = {
                        str(item.output["path"])
                        for item in (*prior_results, *dispatcher.results)
                        if item.tool_name == "apply_patch"
                        and item.status is ToolExecutionStatus.COMPLETED
                        and isinstance(item.output.get("path"), str)
                    }
                    validated_output = output.model_copy(
                        update={
                            "changed_paths": tuple(
                                p for p in output.changed_paths if p in actual_writes
                            )
                        }
                    )
                latest_test = None
                for res in (*prior_results, *dispatcher.results):
                    if res.tool_name == "apply_patch":
                        latest_test = None
                    elif res.tool_name == "run_tests" and res.status is ToolExecutionStatus.COMPLETED:
                        latest_test = res
                if (
                    latest_test is not None
                    and isinstance(latest_test.output, dict)
                    and isinstance(latest_test.output.get("passed"), bool)
                ):
                    validated_output = validated_output.model_copy(
                        update={"verification_passed": latest_test.output["passed"]}
                    )
                else:
                    validated_output = validated_output.model_copy(
                        update={"verification_passed": None}
                    )
                validate_tool_evidence(
                    request.node_name,
                    validated_output,
                    tuple(dispatcher.results),
                    prior_results=prior_results,
                )
            except RuntimeError as error:
                raise ValueError(str(error)) from error

        runtime = AgentRuntime(
            spec,
            self._model_gateway,
            input_type=AgentInvocation,
            output_type=NodeAgentOutput,
            memory=self._memory,
            tool_definitions=definitions,
            tool_dispatcher=dispatcher,
            usage_recorder=self._usage,
            output_validator=require_evidence,
            max_schema_repairs=3,
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
                    "repository": await asyncio.to_thread(
                        default_repository_context, str(self._tool_set.worktree)
                    ),
                    "execution_requirements": (
                        "Complete only the assigned task and current node, not parallel tasks. "
                        "Use repository-relative paths, e.g. src/app.py; use '.' for search_code. "
                        "Read real source before investigating. Mutation stages must "
                        "call apply_patch before returning final output. "
                        "Test stages must call run_tests. Only report "
                        "changed paths and verification backed by successful tool results. "
                        "When required tool evidence for this stage is obtained, stop calling tools "
                        "and immediately return the structured output JSON conforming to NodeAgentOutput. "
                        "Recall summarizes supplied context only; "
                        "it must not claim to have read files, changed code, or run tests. "
                        "Do not invent bug reports, files, issue trackers, or playtest results."
                    ),
                    # Dependency-task handoff summaries resolved at dispatch
                    # time; this is the cross-task context channel.
                    "upstream_summaries": request.input_refs,
                    "prior_summaries": request.prior_summaries,
                    "prior_message_ids": [str(value) for value in request.prior_message_ids],
                    "prior_artifact_ids": [str(value) for value in request.prior_artifact_ids],
                },
            )
        )
        # Fingerprint actual writes even when the model omits changed_paths.
        written_paths = tuple(
            sorted(
                {
                    str(item.output["path"])
                    for item in dispatcher.results
                    if item.tool_name == "apply_patch"
                    and item.status is ToolExecutionStatus.COMPLETED
                    and isinstance(item.output.get("path"), str)
                }
            )
        )
        output = result.output.model_copy(update={"changed_paths": written_paths})
        return output, result.context_memory_ids, result.tool_calls, role

    async def _prior_tool_results(
        self, request: NodeExecutionRequest
    ) -> tuple[ToolCallResult, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ToolExecutionRow)
                    .where(
                        ToolExecutionRow.task_id == request.task_id,
                        ToolExecutionRow.attempt_id == request.attempt_id,
                    )
                    .order_by(ToolExecutionRow.created_at, ToolExecutionRow.id)
                )
            ).all()
        return tuple(_result_from_row(row, replayed=True) for row in rows)

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
            # The graph-state channel is bounded; the full summary remains in
            # the durable artifact and handoff message.
            summary=output.summary[:2_000],
            worktree_fingerprint=await self._worktree_fingerprint(output.changed_paths),
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

    async def _mark_failed(self, request: NodeExecutionRequest, error: Exception) -> None:
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(WorkflowNodeExecutionRow)
                .where(WorkflowNodeExecutionRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if row is not None and row.status == "RUNNING":
                now = utc_now()
                details = _node_failure_details(error)
                row.status = "FAILED"
                row.result = {
                    "error_type": type(error).__name__,
                    "error": details["message"],
                    **details,
                }
                row.completed_at = now
                payload = {
                    "run_id": str(request.run_id),
                    "task_id": str(request.task_id),
                    "attempt_id": str(request.attempt_id),
                    "node": request.node_name,
                    **details,
                }
                event_id = uuid5(
                    NAMESPACE_URL, f"{request.idempotency_key}:failed:{now.isoformat()}"
                )
                await self._repository.append_audit(
                    session, event_id=event_id, event_type="task.node_failed",
                    aggregate_type="task", aggregate_id=request.task_id,
                    payload=payload, correlation_id=request.run_id,
                    causation_id=request.attempt_id,
                )
                await self._repository.enqueue_event(
                    session, event_id=event_id, topic="task-state", payload=payload,
                )

    def _tool_definition(self, name: str) -> ToolDefinition:
        registered = self._registry.resolve(name, "1.0")
        return ToolDefinition(
            name=name,
            description=f"Governed AutoSWE {name} operation in the isolated task worktree.",
            input_schema=registered.argument_schema,
        )


def _node_failure_details(error: Exception) -> dict[str, str]:
    # Provider errors and validation inputs may contain secrets or private source.
    # Publish classifications and recovery guidance rather than exception bodies.
    if isinstance(error, AgentBudgetExceeded):
        return {
            "error_code": "AGENT_BUDGET_EXCEEDED",
            "message": "The agent exhausted its budget before finishing. Reduce task scope "
            "or use a more efficient model before retrying.",
        }
    if isinstance(error, StructuredOutputExhausted):
        return {
            "error_code": "INVALID_MODEL_OUTPUT",
            "message": "The model could not produce valid output backed by tool evidence. "
            "Inspect tool results and model support before retrying.",
        }
    if isinstance(error, GatewayError):
        return {
            "error_code": "MODEL_PROVIDER_ERROR",
            "message": "The model provider failed. Check the model, connection, credentials "
            "and inference timeout before retrying.",
            "failure_class": error.failure_class.value,
        }
    return {
        "error_code": "TASK_EXECUTION_ERROR",
        "message": "The task stage failed. Inspect tool results and worker diagnostics "
        "before retrying.",
    }


def validate_tool_evidence(
    node_name: str,
    output: NodeAgentOutput,
    results: tuple[ToolCallResult, ...],
    *,
    prior_results: tuple[ToolCallResult, ...] = (),
) -> None:
    """Refuse model claims that are not supported by the durable tool outcomes."""
    latest_by_tool: dict[str, ToolCallResult] = {}
    for result in results:
        latest_by_tool[result.tool_name] = result
    mutation_nodes = {"implement", "generate_tests", "refactor", "draft"}
    patch_result = latest_by_tool.get("apply_patch")
    if node_name in mutation_nodes:
        if patch_result is None or patch_result.status is not ToolExecutionStatus.COMPLETED:
            raise RuntimeError("mutation node requires a successful apply_patch result")
        written_paths = {
            result.output.get("path")
            for result in (*prior_results, *results)
            if result.tool_name == "apply_patch" and result.status is ToolExecutionStatus.COMPLETED
        }
        if set(output.changed_paths) - written_paths:
            raise RuntimeError("changed paths must match successful apply_patch results")

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

    if node_name == "investigate" and not any(
        name in latest_by_tool for name in ("read_file", "search_code")
    ):
        raise RuntimeError(
            "investigation requires repository evidence from read_file or search_code"
        )

    test_result = None
    for item in (*prior_results, *results):
        if item.tool_name == "apply_patch":
            test_result = None
        elif item.tool_name == "run_tests":
            test_result = item
    if node_name in {
        "targeted_test",
        "execute",
        "regression_verify",
        "verify",
        "validate_examples",
    }:
        if "run_tests" not in latest_by_tool:
            raise RuntimeError("verification stage requires a governed run_tests result")
    if output.verification_passed is not None:
        if test_result is None or test_result.status is not ToolExecutionStatus.COMPLETED:
            raise RuntimeError("verification claim requires a governed run_tests result")
        passed = test_result.output.get("passed")
        if not isinstance(passed, bool) or passed is not output.verification_passed:
            raise RuntimeError(
                "agent verification claim does not match the governed run_tests result"
            )
