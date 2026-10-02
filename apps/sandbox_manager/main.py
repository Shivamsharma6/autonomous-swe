from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import docker  # type: ignore[import-untyped]
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text

from apps.api.auth import AdminAuthenticator
from apps.api.dependencies import require_admin
from apps.api.middleware import CorrelationMiddleware
from execution.sandbox.boundary import SandboxBoundary, SandboxBoundaryViolation
from execution.sandbox.manager import PostgresSandboxRunStore, SandboxManager
from execution.sandbox.runner import DockerSandboxRunner, SandboxRequest, SandboxResult
from infrastructure.config import Settings
from observability.logging import configure_logging
from observability.metrics import start_metrics_endpoint
from observability.tracing import configure_telemetry
from persistence.database import Database


def _boundary_rejected() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail="sandbox request violates boundary policy",
    )


def create_production_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    database = Database(settings.database_url)
    client = docker.from_env()
    runner = DockerSandboxRunner(client)
    store = PostgresSandboxRunStore(database)
    manager = SandboxManager(runner, store)
    # Isolation-relevant request fields are re-derived here rather than trusted
    # from the caller: this process, not the HTTP body, decides which host paths
    # may be mounted, which image may run, and as which container user.
    boundary = SandboxBoundary.from_settings(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.reconcile_after_worker_restart()
        yield
        await database.dispose()
        client.close()

    application = FastAPI(
        title="AutoSWE Sandbox Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.authenticator = AdminAuthenticator(
        settings.require_sandbox_manager_token()
    )
    application.add_middleware(CorrelationMiddleware, trust_internal_headers=True)

    @application.get("/live")
    async def live() -> dict[str, bool]:
        return {"alive": True}

    @application.get("/ready")
    async def ready() -> dict[str, bool]:
        postgres_ready = False
        docker_ready = False
        try:
            async with database.sessions() as session:
                postgres_ready = bool(await session.scalar(text("SELECT 1")))
        except Exception:
            postgres_ready = False
        try:
            docker_ready = bool(client.ping())
        except docker.errors.DockerException:
            docker_ready = False
        all_ready = postgres_ready and docker_ready
        if not all_ready:
            raise HTTPException(
                status_code=503,
                detail={"postgres": postgres_ready, "docker": docker_ready},
            )
        return {"ready": True, "postgres": True, "docker": True}

    @application.post(
        "/executions",
        response_model=SandboxResult,
        dependencies=[Depends(require_admin)],
    )
    async def execute(request: SandboxRequest) -> SandboxResult:
        try:
            governed = boundary.enforce(request)
        except SandboxBoundaryViolation as exc:
            # 403, not 409: the request is well formed but asks for isolation
            # the platform does not grant, and it must never reach Docker.
            raise _boundary_rejected() from exc
        try:
            return await manager.execute(governed)
        except SandboxBoundaryViolation as exc:
            raise _boundary_rejected() from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="sandbox execution failed") from exc

    @application.post(
        "/executions/{execution_id}/cancel",
        status_code=202,
        dependencies=[Depends(require_admin)],
    )
    async def cancel(execution_id: str) -> dict[str, str]:
        from uuid import UUID

        try:
            parsed = UUID(execution_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid execution ID") from exc
        try:
            cancelled = await manager.cancel(parsed)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc
        return {"execution_id": str(parsed), "status": "CANCELLED" if cancelled else "TERMINAL"}

    configure_telemetry(
        service_name="autoswe-sandbox-manager",
        endpoint=settings.otel_exporter_otlp_endpoint,
        application=application,
        sqlalchemy_engine=database.engine,
    )
    start_metrics_endpoint()
    return application
