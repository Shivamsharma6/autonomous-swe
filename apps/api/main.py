from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from apps.api.auth import AdminAuthenticator
from apps.api.dependencies import ControlPlaneServices, ReadinessChecks
from apps.api.middleware import (
    CorrelationMiddleware,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from apps.api.routes import router
from execution.scheduler.service import ConcurrencyPolicy, SchedulerService
from infrastructure.config import Settings
from knowledge.memory.uams import UAMSMemoryAdapter
from observability.logging import configure_logging
from observability.metrics import start_metrics_endpoint
from observability.tracing import configure_telemetry
from persistence.artifacts import ArtifactService, ArtifactStore
from persistence.database import Database
from persistence.repositories import DomainRepository
from tools.approval import ApprovalService


def create_app(services: ControlPlaneServices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await services.close()

    application = FastAPI(
        title="AutoSWE Production Control Plane",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.services = services
    application.state.authenticator = AdminAuthenticator(
        services.settings.admin_token.get_secret_value()
    )
    application.add_middleware(CorrelationMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=services.settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=600,
    )
    application.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=services.settings.api_rate_limit_per_minute,
    )
    application.add_middleware(
        RequestSizeLimitMiddleware,
        maximum_bytes=services.settings.request_max_bytes,
    )
    application.add_middleware(SecurityHeadersMiddleware)

    @application.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health/ready")
    async def readiness() -> JSONResponse:
        dependencies = await services.readiness.run()
        ready = all(dependencies.values())
        return JSONResponse(
            {"ready": ready, "dependencies": dependencies},
            status_code=200 if ready else 503,
        )

    application.include_router(router)
    return application


def create_production_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    # Process metrics are served on the internal-only Prometheus port instead
    # of an unauthenticated application route.
    start_metrics_endpoint()
    database = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    memory = UAMSMemoryAdapter(
        base_url=settings.uams_url,
        token=settings.uams_token.get_secret_value(),
        timeout=settings.uams_timeout_seconds,
    )
    repository = DomainRepository()
    artifacts = ArtifactService(
        store=ArtifactStore(settings.artifact_root),
        repository=repository,
    )
    scheduler = SchedulerService(
        database=database,
        policy=ConcurrencyPolicy(
            max_parallel_tasks=settings.max_parallel_tasks,
            max_parallel_tasks_per_project=settings.max_parallel_tasks_per_project,
            max_model_concurrency=settings.max_model_concurrency,
            max_sandbox_concurrency=settings.max_sandbox_concurrency,
        ),
        lease_ttl=timedelta(seconds=30),
    )
    http_client = httpx.AsyncClient(timeout=5)

    async def postgres_ready() -> bool:
        async with database.sessions() as session:
            return bool(await session.scalar(text("SELECT 1")))

    async def redis_ready() -> bool:
        return bool(await redis.ping())

    async def checkpoints_ready() -> bool:
        async with database.sessions() as session:
            tables = await session.execute(
                text(
                    "SELECT to_regclass('public.checkpoints'), "
                    "to_regclass('public.checkpoint_writes')"
                )
            )
            row = tables.one()
            return all(value is not None for value in row)

    async def sandbox_ready() -> bool:
        try:
            response = await http_client.get(f"{settings.sandbox_manager_url.rstrip('/')}/ready")
            return response.status_code == 200 and bool(response.json().get("ready"))
        except (httpx.HTTPError, ValueError):
            return False

    async def model_ready() -> bool:
        return bool(settings.model_primary and settings.model_base_url)

    async def no_op_cancel(_: object) -> None:
        return None

    services = ControlPlaneServices(
        settings=settings,
        database=database,
        redis=redis,
        memory=memory,
        approvals=ApprovalService(database=database),
        artifacts=artifacts,
        scheduler=scheduler,
        cancel_notify=no_op_cancel,
        readiness=ReadinessChecks(
            postgres=postgres_ready,
            redis=redis_ready,
            checkpoints=checkpoints_ready,
            sandbox=sandbox_ready,
            model=model_ready,
            uams=memory.ready,
        ),
        database_repository=repository,
        close_callbacks=(http_client.aclose, memory.close, redis.aclose, database.dispose),
    )
    application = create_app(services)
    configure_telemetry(
        service_name="autoswe-api",
        endpoint=settings.otel_exporter_otlp_endpoint,
        application=application,
        sqlalchemy_engine=database.engine,
    )
    return application
