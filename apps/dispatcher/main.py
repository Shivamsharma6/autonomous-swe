from __future__ import annotations

import asyncio
import signal
import socket
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from redis.asyncio import Redis

from agents.gateway import OpenAICompatibleGateway, ProviderCapabilities
from apps.dispatcher.background import CONSUMER_GROUP, EventConsumptionLoop, RetentionLoop
from domain.models import ContractModel, PlanLimits
from execution.sandbox.worktrees import GitWorktreeManager
from execution.scheduler.reconciliation import ReconciliationService
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService, TaskClaim
from infrastructure.config import Settings
from knowledge.memory.uams import UAMSMemoryAdapter
from messaging.outbox import OutboxPublisher
from messaging.redis_streams import RedisStreamsTransport
from observability.logging import configure_logging, get_structured_logger
from observability.metrics import start_metrics_endpoint
from observability.tracing import configure_telemetry
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.database import Database
from persistence.repositories import DomainRepository
from planning.service import RunPlanningService
from workflows.feature import build_scheduler_publish_graph
from workflows.finalization import RunFinalizationService

logger = get_structured_logger("autoswe.dispatcher")


class DispatchMessage(ContractModel):
    schema_version: str = "1.0"
    task_id: UUID
    project_id: UUID
    owner: str
    lease_token: UUID
    attempt_id: UUID
    expires_at: str

    @classmethod
    def from_claim(cls, claim: TaskClaim) -> DispatchMessage:
        return cls(
            task_id=claim.task_id,
            project_id=claim.project_id,
            owner=claim.owner,
            lease_token=claim.token,
            attempt_id=uuid5(NAMESPACE_URL, f"task-attempt:{claim.task_id}:{claim.token}"),
            expires_at=claim.expires_at.isoformat(),
        )


class SchedulerClaimPort(Protocol):
    async def reclaim_expired(self) -> int: ...

    async def claim_ready(self, *, owner: str, limit: int) -> tuple[TaskClaim, ...]: ...


class DispatchPublisher(Protocol):
    async def publish(self, message: DispatchMessage) -> None: ...


class RunPlannerPort(Protocol):
    async def plan_next(self) -> object | None: ...


class RunFinalizerPort(Protocol):
    async def advance_next(self) -> object | None: ...


class ReconcilerPort(Protocol):
    async def reconcile_due(
        self,
        *,
        limit: int = 32,
        now: datetime | None = None,
    ) -> Mapping[UUID, object]: ...


class RedisDispatchPublisher:
    def __init__(self, transport: RedisStreamsTransport) -> None:
        self._transport = transport

    async def publish(self, message: DispatchMessage) -> None:
        await self._transport.publish(
            "task-dispatch",
            message.lease_token,
            message.model_dump(mode="json"),
        )


class DispatcherService:
    def __init__(
        self,
        *,
        scheduler: SchedulerClaimPort,
        publisher: DispatchPublisher,
        owner: str,
        batch_size: int = 32,
        poll_seconds: float = 0.25,
        planner: RunPlannerPort | None = None,
        finalizer: RunFinalizerPort | None = None,
        reconciler: ReconcilerPort | None = None,
    ) -> None:
        if not owner.strip() or batch_size < 1 or poll_seconds <= 0:
            raise ValueError("dispatcher owner, batch size, and poll interval must be valid")
        self._scheduler = scheduler
        self._publisher = publisher
        self._owner = owner
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds
        self._planner = planner
        self._finalizer = finalizer
        self._reconciler = reconciler
        self._dispatch_graph = build_scheduler_publish_graph(self._publish_payload)

    async def _publish_payload(self, payload: dict[str, object]) -> None:
        await self._publisher.publish(DispatchMessage.model_validate(payload))

    async def dispatch_once(self) -> int:
        if self._planner is not None:
            try:
                await self._planner.plan_next()
            except Exception as error:
                logger.error(
                    "run_planning_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        if self._finalizer is not None:
            try:
                await self._finalizer.advance_next()
            except Exception as error:
                logger.error(
                    "run_finalization_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        if self._reconciler is not None:
            await self._reconciler.reconcile_due()
        await self._scheduler.reclaim_expired()
        promote = getattr(self._scheduler, "promote_dependency_ready", None)
        if promote is not None:
            await promote()
        claims = await self._scheduler.claim_ready(owner=self._owner, limit=self._batch_size)
        messages = tuple(DispatchMessage.from_claim(claim) for claim in claims)
        if messages:
            state = await self._dispatch_graph.ainvoke(
                {
                    "dispatch_messages": [
                        message.model_dump(mode="json") for message in messages
                    ],
                    "published_lease_tokens": [],
                }
            )
            expected = sorted(str(message.lease_token) for message in messages)
            if state.get("ordered_lease_tokens") != expected:
                raise RuntimeError("LangGraph dispatch fan-in lost a scheduler lease")
        publish_metrics = getattr(self._scheduler, "publish_metrics", None)
        if publish_metrics is not None:
            await publish_metrics()
        return len(claims)

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                dispatched = await self.dispatch_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                dispatched = 0
                logger.error(
                    "dispatch_cycle_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                    consecutive_failures=failures,
                )
            if dispatched:
                continue
            delay = self._poll_seconds * (2**min(failures, 4))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass


async def run_dispatcher() -> None:
    configure_logging()
    settings = Settings()
    database = Database(settings.database_url)
    start_metrics_endpoint()
    configure_telemetry(
        service_name="autoswe-dispatcher",
        endpoint=settings.otel_exporter_otlp_endpoint,
        sqlalchemy_engine=database.engine,
    )
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
    transport = RedisStreamsTransport(redis)
    repository = DomainRepository()
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
    artifacts = ArtifactService(
        store=ArtifactStore(settings.artifact_root),
        repository=repository,
    )
    worktrees = GitWorktreeManager(settings.managed_worktree_root)
    owner = f"dispatcher:{socket.gethostname()}"
    service = DispatcherService(
        scheduler=scheduler,
        publisher=RedisDispatchPublisher(transport),
        owner=owner,
        planner=RunPlanningService(
            database=database,
            gateway=model,
            memory=memory,
            primary_model=settings.model_primary,
            fallback_models=tuple(settings.model_fallbacks),
            limits=PlanLimits(
                max_dynamic_tasks=settings.max_dynamic_tasks,
                max_plan_depth=settings.max_plan_depth,
                max_total_budget_usd=settings.max_total_budget_usd,
                max_total_execution_seconds=settings.max_total_execution_seconds,
            ),
            repository=repository,
        ),
        finalizer=RunFinalizationService(
            database=database,
            gateway=model,
            memory=memory,
            artifacts=artifacts,
            scheduler=scheduler,
            worktrees=worktrees,
            primary_model=settings.model_primary,
            fallback_models=tuple(settings.model_fallbacks),
            limits=PlanLimits(
                max_dynamic_tasks=settings.max_dynamic_tasks,
                max_plan_depth=settings.max_plan_depth,
                max_total_budget_usd=settings.max_total_budget_usd,
                max_total_execution_seconds=settings.max_total_execution_seconds,
            ),
            repository=repository,
        ),
        reconciler=ReconciliationService(database=database, repository=repository),
    )
    outbox = OutboxPublisher(database, transport, publisher_id=owner)
    events = EventConsumptionLoop(
        database=database,
        transport=transport,
        consumer_name=f"events:{owner}",
        streams=("task-state", "workflow-state", "artifact-integrity", "reconciliation"),
    )
    retention = RetentionLoop(database=database, transport=transport)
    stop = asyncio.Event()
    _install_signal_handlers(stop.set)

    async def publish_outbox() -> None:
        while not stop.is_set():
            published = await outbox.publish_batch(limit=100)
            if not published:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass

    async def publish_task_dispatch() -> None:
        # Re-dispatch after crashes is owned by Postgres lease expiry plus
        # reconciliation; this loop only reclaims stale pending entries so the
        # consumer PEL does not grow forever.
        await transport.ensure_group("task-dispatch", CONSUMER_GROUP)
        while not stop.is_set():
            try:
                stale = await transport.reclaim(
                    "task-dispatch",
                    CONSUMER_GROUP,
                    f"dispatch-reaper:{owner}",
                    min_idle=timedelta(minutes=5),
                    count=32,
                )
                for record in stale:
                    await transport.acknowledge(
                        "task-dispatch", CONSUMER_GROUP, record.stream_id
                    )
                if stale:
                    logger.info(
                        "stale_dispatch_entries_reclaimed",
                        count=len(stale),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "dispatch_reclaim_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=30.0)
            except TimeoutError:
                pass

    try:
        await asyncio.gather(
            service.run(stop),
            publish_outbox(),
            events.run(stop),
            retention.run(stop),
            publish_task_dispatch(),
        )
    finally:
        await model.close()
        await memory.close()
        await redis.aclose()
        await database.dispose()


def _install_signal_handlers(callback: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, callback)
        except NotImplementedError:
            signal.signal(signum, lambda *_: callback())


if __name__ == "__main__":
    asyncio.run(run_dispatcher())
