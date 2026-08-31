from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from agents.gateway import (
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ModelUsage,
    ProviderCapabilities,
    ToolCall,
)
from apps.api.dependencies import ControlPlaneServices, ReadinessChecks
from apps.api.main import create_app
from apps.dispatcher.main import DispatcherService, RedisDispatchPublisher
from apps.worker.executor import DispatchedTaskExecutor, TaskExecutionContext
from apps.worker.nodes import ProductionNodeExecutor
from apps.worker.runner import RedisDispatchInbox, WorkerService
from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.models import (
    BudgetPolicy,
    PlanLimits,
    ResourceEstimate,
    TaskPlan,
    TaskPlanMutation,
    TaskSpec,
    canonical_sha256,
)
from execution.sandbox.manager import PostgresSandboxRunStore, SandboxManager
from execution.sandbox.runner import DockerSandboxRunner, SandboxRequest, SandboxResult
from execution.sandbox.worktrees import GitWorktreeManager
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from infrastructure.config import Settings
from knowledge.memory.uams import UAMSMemoryAdapter
from messaging.redis_streams import RedisStreamsTransport
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.repositories import DomainRepository
from persistence.tables import (
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    MemoryCandidateRow,
    ModelCallRow,
    PlanRevisionRow,
    RepairMutationRow,
    RunRow,
    SandboxExecutionRow,
    TaskRow,
)
from planning.service import RunPlanningService
from tools.approval import ApprovalService
from tools.production import ProductionToolSet, SandboxManagerClient
from workflows.checkpoints import postgres_checkpointer
from workflows.finalization import RunFinalizationService

pytestmark = pytest.mark.asyncio

UV_IMAGE = (
    "ghcr.io/astral-sh/uv@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca"
)
NODE_IMAGE = "node@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436"
ADMIN_TOKEN = "e2e-" + "a" * 40
GIT = shutil.which("git")


def git(repository: Path, *arguments: str) -> str:
    if GIT is None:
        pytest.skip("Git is unavailable")
    return subprocess.run(  # noqa: S603 - isolated repository and fixed Git argument arrays
        (GIT, "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


class ScriptedScenarioGateway:
    """Trace-keyed scripted model that remains deterministic under parallel fan-out."""

    def __init__(
        self,
        *,
        plan: TaskPlan,
        mutation: TaskPlanMutation,
        parallel_roots: frozenset[UUID],
    ) -> None:
        self.plan = plan
        self.mutation = mutation
        self.parallel_roots = parallel_roots
        self.requests: list[ModelRequest] = []
        self.tool_results: list[dict[str, Any]] = []
        self.release_criteria: tuple[str, ...] = ()
        self._parallel_started: set[UUID] = set()
        self._parallel_gate = asyncio.Event()
        self._lock = asyncio.Lock()
        self.max_parallel_root_recalls = 0

    async def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities.all_supported()

    async def complete(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        if request.output_schema_name == "TaskPlan":
            return self._response(request, self.plan.model_dump(mode="json"))
        if request.output_schema_name == "TaskPlanMutation":
            return self._response(request, self.mutation.model_dump(mode="json"))
        if request.output_schema_name == "ReleaseDecision":
            payload = _input_payload(request)
            evidence_ids = payload["verified_artifact_ids"]
            evidence = evidence_ids[-1]
            self.release_criteria = tuple(payload["acceptance_criteria"])
            return self._response(
                request,
                {
                    "approved": True,
                    "summary": "All acceptance criteria pass with verified final evidence.",
                    "acceptance_evidence": {
                        criterion: [evidence] for criterion in payload["acceptance_criteria"]
                    },
                    "failure_reasons": [],
                },
            )

        task_id, node = _trace_identity(request.trace_id)
        await self._prove_parallel_root_fanout(task_id, node)
        tool_result = _latest_tool_result(request)
        if tool_result is not None:
            self.tool_results.append(tool_result)
        call = self._tool_call(task_id, node, has_result=tool_result is not None)
        if call is not None:
            return self._response(request, None, tool_calls=(call,))

        passed: bool | None = None
        summary = f"{node} completed with structured evidence"
        if node in {"verify", "targeted_test"} and tool_result is not None:
            output = tool_result.get("output", {})
            passed = bool(output.get("passed"))
            summary = (
                "verification passed in the governed sandbox"
                if passed
                else "intentional integration test failure reproduced in the governed sandbox"
            )
        if task_id == self.plan.tasks[-1].id and node == "evidence":
            passed = False
            summary = "intentional integration failure requires debugger repair"
        if task_id == self.mutation.tasks[-1].id and node == "evidence":
            passed = True
            summary = "repaired integration verification passed"
        return self._response(
            request,
            {
                "summary": summary,
                "evidence": [f"trace:{request.trace_id}"],
                "changed_paths": _changed_paths(task_id, node, self.plan, self.mutation),
                "verification_passed": passed,
            },
        )

    async def stream(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[ModelStreamChunk]:
        if False:
            yield ModelStreamChunk(trace_id=request.trace_id, text="")
        raise NotImplementedError("the deterministic scenario uses structured completion")

    async def _prove_parallel_root_fanout(self, task_id: UUID, node: str) -> None:
        if node != "recall" or task_id not in self.parallel_roots:
            return
        async with self._lock:
            self._parallel_started.add(task_id)
            self.max_parallel_root_recalls = max(
                self.max_parallel_root_recalls,
                len(self._parallel_started),
            )
            if self._parallel_started == set(self.parallel_roots):
                self._parallel_gate.set()
        await asyncio.wait_for(self._parallel_gate.wait(), timeout=10)

    def _tool_call(self, task_id: UUID, node: str, *, has_result: bool) -> ToolCall | None:
        if has_result:
            return None
        research, implementation, tests, initial_validation = self.plan.tasks
        repair, final_validation = self.mutation.tasks
        if task_id == research.id and node == "investigate":
            return ToolCall(
                call_id=f"read-{task_id}",
                name="read_file",
                arguments={"path": "pyproject.toml"},
            )
        if task_id == implementation.id and node == "implement":
            return ToolCall(
                call_id=f"implement-{task_id}",
                name="apply_patch",
                arguments={
                    "path": "src/calc.py",
                    "content": "def add(left: int, right: int) -> int:\n    return left - right\n",
                },
            )
        if task_id == tests.id and node == "generate_tests":
            return ToolCall(
                call_id=f"tests-{task_id}",
                name="apply_patch",
                arguments={
                    "path": "tests/test_calc.py",
                    "content": (
                        "import unittest\n\n"
                        "from src.calc import add\n\n"
                        "class AddTest(unittest.TestCase):\n"
                        "    def test_adds_two_values(self) -> None:\n"
                        "        self.assertEqual(add(2, 3), 5)\n"
                    ),
                },
            )
        if task_id in {initial_validation.id, final_validation.id} and node == "verify":
            return ToolCall(
                call_id=f"verify-{task_id}",
                name="run_tests",
                arguments={"operation": "full_test"},
            )
        if task_id == repair.id and node == "implement":
            return ToolCall(
                call_id=f"repair-{task_id}",
                name="apply_patch",
                arguments={
                    "path": "src/calc.py",
                    "content": "def add(left: int, right: int) -> int:\n    return left + right\n",
                },
            )
        if task_id == repair.id and node == "targeted_test":
            return ToolCall(
                call_id=f"repair-test-{task_id}",
                name="run_tests",
                arguments={"operation": "full_test"},
            )
        if node in {"targeted_test", "execute", "regression_verify"}:
            return ToolCall(
                call_id=f"test-{task_id}-{node}",
                name="run_tests",
                arguments={"operation": "full_test"},
            )
        return None

    @staticmethod
    def _response(
        request: ModelRequest,
        output: dict[str, Any] | None,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> ModelResponse:
        return ModelResponse(
            trace_id=request.trace_id,
            provider_request_id=f"scripted-{len(request.messages)}",
            model=request.model,
            structured_output=output,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=ModelUsage(input_tokens=25, output_tokens=12, cost_usd=0.0001),
        )


def _input_payload(request: ModelRequest) -> dict[str, Any]:
    marker = "Input:\n"
    user = next(message.content for message in request.messages if message.role == "user")
    payload = user.split(marker, 1)[1].split("\n\nVerified UAMS context:", 1)[0]
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _trace_identity(trace_id: str) -> tuple[UUID, str]:
    task_match = re.search(r"task:([0-9a-f-]{36}):", trace_id)
    node_match = re.search(r":node:([a-z_]+)$", trace_id)
    if task_match is None or node_match is None:
        raise AssertionError(f"unexpected task trace: {trace_id}")
    return UUID(task_match.group(1)), node_match.group(1)


def _latest_tool_result(request: ModelRequest) -> dict[str, Any] | None:
    for message in reversed(request.messages):
        if message.role == "tool":
            parsed = json.loads(message.content)
            assert isinstance(parsed, dict)
            return parsed
    return None


def _changed_paths(
    task_id: UUID,
    node: str,
    plan: TaskPlan,
    mutation: TaskPlanMutation,
) -> list[str]:
    if node == "implement" and task_id in {plan.tasks[1].id, mutation.tasks[0].id}:
        return ["src/calc.py"]
    if node == "generate_tests" and task_id == plan.tasks[2].id:
        return ["tests/test_calc.py"]
    return []


def task(
    *,
    task_id: UUID,
    revision: int,
    project_id: UUID,
    repository_id: UUID,
    title: str,
    task_type: TaskType,
    dependencies: tuple[UUID, ...] = (),
    tools: tuple[str, ...] = (),
    criterion: str,
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        plan_revision=revision,
        project_id=project_id,
        repository_id=repository_id,
        title=title,
        description=f"{title} with durable evidence.",
        task_type=task_type,
        dependencies=dependencies,
        assigned_capability=task_type.value.casefold(),
        acceptance_criteria=(criterion,),
        allowed_tools=tools,
        risk_ceiling=RiskLevel.MEDIUM,
        budget=BudgetPolicy(model_tokens=2_000, cost_usd=1, wall_time_seconds=300),
        estimate=ResourceEstimate(model_tokens=1_000, sandbox_slots=1),
    )


def scenario(
    project_id: UUID,
    repository_id: UUID,
    run_id: UUID,
    baseline: str,
    limits: PlanLimits,
) -> tuple[TaskPlan, TaskPlanMutation]:
    research_id, implementation_id, tests_id, validation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    research = task(
        task_id=research_id,
        revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Inspect repository constraints",
        task_type=TaskType.RESEARCH,
        tools=("read_file", "search_code"),
        criterion="Repository constraints are evidenced",
    )
    implementation = task(
        task_id=implementation_id,
        revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Implement addition utility",
        task_type=TaskType.IMPLEMENTATION,
        tools=("read_file", "apply_patch", "run_tests"),
        criterion="Addition utility is implemented",
    )
    tests = task(
        task_id=tests_id,
        revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Create behavioral tests",
        task_type=TaskType.TEST,
        tools=("read_file", "apply_patch", "run_tests"),
        criterion="Behavioral test coverage exists",
    )
    validation = task(
        task_id=validation_id,
        revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Integrate and verify initial plan",
        task_type=TaskType.VALIDATION,
        dependencies=(research_id, implementation_id, tests_id),
        tools=("read_file", "search_code", "run_tests"),
        criterion="Integrated verification passes",
    )
    plan = TaskPlan(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit=baseline,
        revision=1,
        tasks=(research, implementation, tests, validation),
        limits=limits,
    )
    repair_id, final_validation_id = uuid4(), uuid4()
    repair = task(
        task_id=repair_id,
        revision=2,
        project_id=project_id,
        repository_id=repository_id,
        title="Repair reproduced arithmetic failure",
        task_type=TaskType.IMPLEMENTATION,
        dependencies=(validation_id,),
        tools=("read_file", "apply_patch", "run_tests"),
        criterion="The reproduced arithmetic failure is repaired",
    )
    final_validation = task(
        task_id=final_validation_id,
        revision=2,
        project_id=project_id,
        repository_id=repository_id,
        title="Verify repaired integrated repository",
        task_type=TaskType.VALIDATION,
        dependencies=(repair_id,),
        tools=("read_file", "search_code", "run_tests"),
        criterion="Repaired integration verification passes",
    )
    return plan, TaskPlanMutation(
        base_revision=1,
        reason="The initial integration test deterministically reproduced an arithmetic defect.",
        tasks=(repair, final_validation),
    )


async def test_scripted_branching_repair_approval_and_uams_promotion_use_production_engine(
    database: Any,
    postgres_urls: tuple[str, str],
    redis_client: Any,
    tmp_path: Path,
) -> None:
    try:
        docker_client = docker.from_env()
        docker_client.images.get(UV_IMAGE)
        docker_client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"controlled Docker runner image is unavailable: {exc}")

    source = tmp_path / "imports" / "showcase"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "scripted-showcase"\nversion = "0.1.0"\ndependencies = []\n'
    )
    (source / "requirements.txt").write_text("")
    (source / "src").mkdir()
    (source / "src" / "__init__.py").write_text("")
    (source / "tests").mkdir()
    (source / "tests" / "__init__.py").write_text("")
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Scripted E2E")
    git(source, "add", ".")
    git(source, "commit", "-m", "baseline")
    baseline = git(source, "rev-parse", "HEAD")

    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    limits = PlanLimits(
        max_dynamic_tasks=4,
        max_plan_depth=8,
        max_total_budget_usd=20,
        max_total_execution_seconds=4_000,
    )
    plan, mutation = scenario(project_id, repository_id, run_id, baseline, limits)
    gateway = ScriptedScenarioGateway(
        plan=plan,
        mutation=mutation,
        parallel_roots=frozenset(task.id for task in plan.tasks[:3]),
    )
    uams_writes: list[UUID] = []
    uams_revision = str(uuid4())

    async def uams_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if request.method == "POST" and request.url.path == "/search":
            return httpx.Response(200, json={"results": []})
        if request.method == "GET" and request.url.path.startswith("/memory/status/"):
            memory_id = UUID(request.url.path.rsplit("/", 1)[1])
            if memory_id not in uams_writes:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={
                    "current_revision_id": uams_revision,
                    "latest_revision_id": uams_revision,
                    "revision_state": "active",
                    "document_status": "active",
                    "index_status": "indexed",
                },
            )
        if request.method == "POST" and request.url.path == "/remember":
            document = json.loads(request.content)["text"]
            match = re.search(r"^memory_id: ([0-9a-f-]{36})$", document, re.MULTILINE)
            assert match is not None
            memory_id = UUID(match.group(1))
            uams_writes.append(memory_id)
            return httpx.Response(
                200,
                json={"memory_id": str(memory_id), "index_status": "pending"},
            )
        raise AssertionError(f"unexpected UAMS request: {request.method} {request.url.path}")

    uams_client = httpx.AsyncClient(
        transport=httpx.MockTransport(uams_handler),
        base_url="http://uams.test",
    )
    memory = UAMSMemoryAdapter(base_url="http://uams.test", client=uams_client)
    repository = DomainRepository()
    artifacts = ArtifactService(
        store=ArtifactStore(tmp_path / "artifacts"),
        repository=repository,
    )
    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=3,
            max_parallel_tasks_per_project=3,
            max_model_concurrency=3,
            max_sandbox_concurrency=3,
        ),
        lease_ttl=timedelta(minutes=5),
        repository=repository,
    )
    worktrees = GitWorktreeManager(tmp_path / "worktrees")

    sandbox_manager = SandboxManager(
        DockerSandboxRunner(docker_client),
        PostgresSandboxRunStore(database, repository=repository),
    )
    sandbox_app = FastAPI()

    @sandbox_app.post("/executions", response_model=SandboxResult)
    async def execute_sandbox(request: SandboxRequest) -> SandboxResult:
        return await sandbox_manager.execute(request)

    sandbox_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sandbox_app),
        base_url="http://sandbox.test",
        timeout=120,
    )
    sandbox = SandboxManagerClient(
        base_url="http://sandbox.test",
        token=ADMIN_TOKEN,
        client=sandbox_http,
    )

    settings = Settings.model_validate(
        {
            "autoswe_env": "test",
            "admin_token": ADMIN_TOKEN,
            "database_url": "test://postgres",
            "redis_url": "test://redis",
            "uams_url": "http://uams.test",
            "model_base_url": "scripted://model",
            "model_primary": "scripted-model",
            "cors_origins": ["http://console.test"],
            "repository_import_root": source.parent,
            "python_runner_image": "test://python",
            "node_runner_image": "test://node",
        }
    )

    async def ready() -> bool:
        return True

    async def notify(_: UUID) -> None:
        return None

    services = ControlPlaneServices(
        settings=settings,
        database=database,
        redis=redis_client,
        memory=memory,
        approvals=ApprovalService(database=database, repository=repository),
        artifacts=artifacts,
        scheduler=scheduler,
        cancel_notify=notify,
        readiness=ReadinessChecks(
            postgres=ready,
            redis=ready,
            checkpoints=ready,
            sandbox=ready,
            model=ready,
            uams=memory.ready,
        ),
        database_repository=repository,
    )
    api = create_app(services)
    api_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://control.test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )

    planning = RunPlanningService(
        database=database,
        gateway=gateway,
        memory=memory,
        primary_model="scripted-model",
        fallback_models=(),
        limits=limits,
        repository=repository,
    )
    finalization = RunFinalizationService(
        database=database,
        gateway=gateway,
        memory=memory,
        artifacts=artifacts,
        scheduler=scheduler,
        worktrees=worktrees,
        primary_model="scripted-model",
        fallback_models=(),
        limits=limits,
        repository=repository,
    )
    transport = RedisStreamsTransport(redis_client)
    dispatcher = DispatcherService(
        scheduler=scheduler,
        publisher=RedisDispatchPublisher(transport),
        owner="dispatcher:e2e",
        batch_size=3,
        poll_seconds=0.01,
        planner=planning,
        finalizer=finalization,
    )

    async def node_factory(context: TaskExecutionContext) -> ProductionNodeExecutor:
        task_worktree = await asyncio.to_thread(
            worktrees.create_task_worktree,
            Path(context.source_path),
            context.task_id,
            context.baseline_commit,
        )
        if context.dependencies:
            await asyncio.to_thread(
                worktrees.integrate_task_dependencies,
                Path(context.source_path),
                task_worktree,
                context.dependencies,
            )
        tool_set = ProductionToolSet(
            source_repository=Path(context.source_path),
            worktree=task_worktree,
            run_id=context.run_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            sandbox=sandbox,
            python_image=UV_IMAGE,
            node_image=NODE_IMAGE,
            uid=max(1, os.getuid()),
            gid=max(1, os.getgid()),
        )
        return ProductionNodeExecutor(
            database=database,
            memory=memory,
            model_gateway=gateway,
            tool_set=tool_set,
            project_id=context.project_id,
            repository_id=context.repository_id,
            baseline_commit=context.baseline_commit,
            allowed_tools=context.allowed_tools,
            assigned_capability=context.assigned_capability,
            risk_ceiling=context.risk_ceiling,
            primary_model="scripted-model",
            fallback_models=(),
            artifacts=artifacts,
            repository=repository,
        )

    @asynccontextmanager
    async def checkpointer() -> AsyncIterator[Any]:
        async with postgres_checkpointer(postgres_urls[1]) as saver:
            yield saver

    executor = DispatchedTaskExecutor(
        database=database,
        scheduler=scheduler,
        node_executor_factory=node_factory,
        checkpointer_factory=checkpointer,
        production_graph=True,
        agent_spec_hash=canonical_sha256({"engine": "scripted-production-e2e"}),
        heartbeat_seconds=5,
    )
    inboxes = tuple(
        RedisDispatchInbox(
            transport,
            group="autoswe-e2e-workers",
            consumer=f"worker:e2e:{index}",
        )
        for index in range(3)
    )
    for inbox in inboxes:
        await inbox.setup()

    async def process_wave(size: int) -> None:
        processed = await asyncio.gather(
            *(
                WorkerService(inbox=inboxes[index], executor=executor).process_once()
                for index in range(size)
            )
        )
        assert sum(processed) == size

    try:
        async with postgres_checkpointer(postgres_urls[1], setup=True):
            pass
        project_response = await api_client.post(
            "/api/v1/projects",
            json={
                "project_id": str(project_id),
                "repository_id": str(repository_id),
                "name": "Scripted production E2E",
                "source_path": str(source),
            },
        )
        assert project_response.status_code == 201
        run_response = await api_client.post(
            "/api/v1/runs",
            json={
                "run_id": str(run_id),
                "project_id": str(project_id),
                "repository_id": str(repository_id),
                "goal": "Implement and verify a correct integer addition utility",
                "baseline_commit": baseline,
            },
        )
        assert run_response.status_code == 202

        assert await dispatcher.dispatch_once() == 3
        await process_wave(3)
        assert gateway.max_parallel_root_recalls == 3

        assert await dispatcher.dispatch_once() == 1
        await process_wave(1)
        assert await dispatcher.dispatch_once() == 1
        await process_wave(1)
        assert await dispatcher.dispatch_once() == 1
        await process_wave(1)

        async with database.sessions() as session:
            latest_tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRow).where(
                            TaskRow.run_id == run_id,
                            TaskRow.plan_revision == 2,
                        )
                    )
                ).all()
            )
        latest_failures, _ = await finalization._verification_evidence(  # noqa: SLF001
            latest_tasks
        )
        assert latest_failures == (), json.dumps(gateway.tool_results, indent=2)
        assert await dispatcher.dispatch_once() == 0
        approvals_response = await api_client.get(f"/api/v1/runs/{run_id}/approvals")
        assert approvals_response.status_code == 200
        approval = approvals_response.json()[0]
        assert approval["status"] == "PENDING"
        assert approval["tool_name"] == "git_commit"
        decision = await api_client.post(
            f"/api/v1/approvals/{approval['approval_id']}/decision",
            json={
                "approved": True,
                "approver": "e2e-operator@example.invalid",
                "expected_call_hash": approval["call_hash"],
            },
        )
        assert decision.status_code == 202
        assert await dispatcher.dispatch_once() == 0

        status = await api_client.get(f"/api/v1/runs/{run_id}")
        tasks = await api_client.get(f"/api/v1/runs/{run_id}/tasks")
        artifacts_response = await api_client.get(f"/api/v1/runs/{run_id}/artifacts")
        events = await api_client.get(f"/api/v1/runs/{run_id}/events")
        assert status.json()["state"] == "COMPLETED"
        assert status.json()["active_plan_revision"] == 2
        assert all(item["state"] == TaskStatus.COMPLETED.value for item in tasks.json())
        assert len(tasks.json()) == 6
        assert all(item["state"] == "VALID" for item in artifacts_response.json())
        event_types = {item["event_type"] for item in events.json()}
        assert {
            "plan.created",
            "plan.repair_accepted",
            "release.reviewed",
            "memory.promoted",
        }.issubset(event_types)

        async with database.sessions() as session:
            run = await session.get(RunRow, run_id)
            revisions = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PlanRevisionRow)
                    .where(PlanRevisionRow.run_id == run_id)
                )
                or 0
            )
            repairs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RepairMutationRow)
                    .where(RepairMutationRow.run_id == run_id)
                )
                or 0
            )
            sandbox_runs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SandboxExecutionRow)
                    .join(TaskRow, TaskRow.id == SandboxExecutionRow.task_id)
                    .where(TaskRow.run_id == run_id)
                )
                or 0
            )
            model_calls = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelCallRow)
                    .where(ModelCallRow.run_id == run_id)
                )
                or 0
            )
            memory_row = await session.scalar(
                select(MemoryCandidateRow).where(MemoryCandidateRow.run_id == run_id)
            )
            release_artifact = await session.scalar(
                select(ArtifactRow).where(
                    ArtifactRow.run_id == run_id,
                    ArtifactRow.media_type == "application/vnd.autoswe.release-decision+json",
                )
            )
            repair_event = await session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.correlation_id == run_id,
                    AuditEventRow.event_type == "plan.repair_accepted",
                )
            )
            approval_row = await session.scalar(
                select(ApprovalRow).where(ApprovalRow.call_hash == approval["call_hash"])
            )
        assert run is not None and run.state == "COMPLETED"
        assert revisions == 2 and repairs == 1
        assert sandbox_runs >= 7
        assert model_calls == len(gateway.requests)
        assert memory_row is not None and memory_row.status == "PROMOTED"
        assert release_artifact is not None and release_artifact.verified_at is not None
        assert repair_event is not None
        assert approval_row is not None and approval_row.approver == "e2e-operator@example.invalid"
        assert len(uams_writes) == 1
        expected_criteria = {
            criterion
            for item in (*plan.tasks, *mutation.tasks)
            for criterion in item.acceptance_criteria
        }
        assert set(gateway.release_criteria) == expected_criteria

        commit = git(source, "rev-parse", f"autoswe/task/{mutation.tasks[-1].id}")
        committed_source = git(source, "show", f"{commit}:src/calc.py")
        committed_test = git(source, "show", f"{commit}:tests/test_calc.py")
        assert "left + right" in committed_source
        assert "self.assertEqual(add(2, 3), 5)" in committed_test
        assert not worktrees.managed_root.joinpath(f"task-{mutation.tasks[-1].id}").exists()
        observed_checks = tuple(
            result.get("output", {}).get("passed") for result in gateway.tool_results
        )
        assert False in observed_checks
        assert True in observed_checks
    finally:
        await api_client.aclose()
        await sandbox_http.aclose()
        await uams_client.aclose()
        docker_client.close()
