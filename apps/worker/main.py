from __future__ import annotations

import asyncio
import socket
from datetime import timedelta
from pathlib import Path

from redis.asyncio import Redis

from agents.gateway import OpenAICompatibleGateway, ProviderCapabilities
from apps.worker.executor import DispatchedTaskExecutor, TaskExecutionContext
from apps.worker.nodes import ProductionNodeExecutor
from apps.worker.runner import RedisDispatchInbox, WorkerService, install_worker_signal_handlers
from domain.models import canonical_sha256
from execution.sandbox.worktrees import GitWorktreeManager
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from infrastructure.config import Settings
from knowledge.memory.uams import UAMSMemoryAdapter
from messaging.redis_streams import RedisStreamsTransport
from observability.logging import configure_logging
from observability.metrics import start_metrics_endpoint
from observability.tracing import configure_telemetry
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.database import Database
from persistence.repositories import DomainRepository
from policies.risk.policy_engine import risk_exceeds
from tools.production import ProductionToolSet, SandboxManagerClient
from workflows.checkpoints import postgres_checkpointer


async def run_worker() -> None:
    configure_logging()
    settings = Settings()
    database = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    memory = UAMSMemoryAdapter(
        base_url=settings.uams_url,
        token=settings.uams_token.get_secret_value(),
        timeout=settings.uams_timeout_seconds,
    )
    declared = (
        ProviderCapabilities.all_supported()
        if settings.model_capability_mode == "declared"
        else None
    )
    model = OpenAICompatibleGateway(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key.get_secret_value(),
        max_concurrency=settings.max_model_concurrency,
        input_cost_per_million=settings.model_input_cost_per_million,
        cached_input_cost_per_million=settings.model_cached_input_cost_per_million,
        output_cost_per_million=settings.model_output_cost_per_million,
        default_capabilities=declared,
    )
    sandbox = SandboxManagerClient(
        base_url=settings.sandbox_manager_url,
        token=settings.admin_token.get_secret_value(),
    )
    repository = DomainRepository()
    artifacts = ArtifactService(
        store=ArtifactStore(settings.artifact_root),
        repository=repository,
    )
    worktrees = GitWorktreeManager(settings.managed_worktree_root)
    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=settings.max_parallel_tasks,
            max_parallel_tasks_per_project=settings.max_parallel_tasks_per_project,
            max_model_concurrency=settings.max_model_concurrency,
            max_sandbox_concurrency=settings.max_sandbox_concurrency,
        ),
        lease_ttl=timedelta(seconds=30),
        repository=repository,
    )

    async def node_executor_factory(context: TaskExecutionContext) -> ProductionNodeExecutor:
        source = Path(context.source_path)
        worktree = await asyncio.to_thread(
            worktrees.create_task_worktree,
            source,
            context.task_id,
            context.baseline_commit,
        )
        if context.dependencies:
            await asyncio.to_thread(
                worktrees.integrate_task_dependencies,
                source,
                worktree,
                context.dependencies,
            )
        tool_set = ProductionToolSet(
            source_repository=source,
            worktree=worktree,
            run_id=context.run_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            sandbox=sandbox,
            python_image=settings.python_runner_image,
            node_image=settings.node_runner_image,
            uid=settings.host_uid,
            gid=settings.host_gid,
        )
        return ProductionNodeExecutor(
            database=database,
            memory=memory,
            model_gateway=model,
            tool_set=tool_set,
            project_id=context.project_id,
            repository_id=context.repository_id,
            baseline_commit=context.baseline_commit,
            allowed_tools=context.allowed_tools,
            assigned_capability=context.assigned_capability,
            risk_ceiling=(
                settings.max_risk_ceiling
                if risk_exceeds(context.risk_ceiling, settings.max_risk_ceiling)
                else context.risk_ceiling
            ),
            primary_model=settings.model_primary,
            fallback_models=tuple(settings.model_fallbacks),
            artifacts=artifacts,
            repository=repository,
        )

    executor = DispatchedTaskExecutor(
        database=database,
        scheduler=scheduler,
        node_executor_factory=node_executor_factory,
        checkpointer_factory=lambda: postgres_checkpointer(settings.database_url),
        production_graph=True,
        agent_spec_hash=canonical_sha256(
            {
                "engine": "production-task-subgraphs@1.0",
                "primary_model": settings.model_primary,
                "fallback_models": settings.model_fallbacks,
            }
        ),
    )
    transport = RedisStreamsTransport(redis)
    inbox = RedisDispatchInbox(
        transport,
        group="autoswe-workers",
        consumer=f"worker:{socket.gethostname()}",
    )
    stop = asyncio.Event()
    install_worker_signal_handlers(stop)
    start_metrics_endpoint()
    configure_telemetry(
        service_name="autoswe-worker",
        endpoint=settings.otel_exporter_otlp_endpoint,
        sqlalchemy_engine=database.engine,
    )
    try:
        await WorkerService(inbox=inbox, executor=executor).run(stop)
    finally:
        await sandbox.close()
        await model.close()
        await memory.close()
        await redis.aclose()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(run_worker())
