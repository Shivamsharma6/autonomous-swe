from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.auth import AdminAuthenticator, AdminPrincipal, AuthenticationError
from infrastructure.config import Settings
from knowledge.memory.port import MemoryPort
from observability.logging import get_structured_logger
from persistence.artifacts import ArtifactService
from persistence.database import Database
from persistence.repositories import DomainRepository
from tools.approval import ApprovalService

logger = get_structured_logger("autoswe.api.auth")

ReadinessProbe = Callable[[], Awaitable[bool]]
CloseCallback = Callable[[], Awaitable[None]]
CancelNotification = Callable[[UUID], Awaitable[None]]


class RedisHealthPort(Protocol):
    def ping(self) -> Awaitable[bool]: ...


class SchedulerCancellationPort(Protocol):
    async def cancel_task(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        notify: CancelNotification,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReadinessChecks:
    postgres: ReadinessProbe
    redis: ReadinessProbe
    checkpoints: ReadinessProbe
    sandbox: ReadinessProbe
    model: ReadinessProbe
    uams: ReadinessProbe
    timeout_seconds: float = 5.0

    async def run(self) -> dict[str, bool]:
        names = ("postgres", "redis", "checkpoints", "sandbox", "model", "uams")
        probes = tuple(getattr(self, name) for name in names)

        async def bounded(probe: ReadinessProbe) -> bool:
            try:
                return bool(await asyncio.wait_for(probe(), timeout=self.timeout_seconds))
            except Exception:
                return False

        values = await asyncio.gather(*(bounded(probe) for probe in probes))
        return dict(zip(names, values, strict=True))


@dataclass(slots=True)
class ControlPlaneServices:
    settings: Settings
    database: Database
    redis: RedisHealthPort
    memory: MemoryPort
    approvals: ApprovalService
    artifacts: ArtifactService
    scheduler: SchedulerCancellationPort
    cancel_notify: CancelNotification
    readiness: ReadinessChecks
    database_repository: DomainRepository = field(default_factory=DomainRepository)
    close_callbacks: tuple[CloseCallback, ...] = field(default_factory=tuple)

    async def close(self) -> None:
        for callback in reversed(self.close_callbacks):
            await callback()


def get_services(request: Request) -> ControlPlaneServices:
    return cast(ControlPlaneServices, request.app.state.services)


_bearer = HTTPBearer(auto_error=False)
BearerDependency = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


async def require_admin(
    request: Request,
    credentials: BearerDependency,
) -> AdminPrincipal:
    token = (
        credentials.credentials
        if credentials and credentials.scheme.lower() == "bearer"
        else ""
    )
    authenticator: AdminAuthenticator = request.app.state.authenticator
    try:
        return authenticator.authenticate(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid administrator credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def require_websocket_admin(websocket: WebSocket) -> AdminPrincipal:
    # Tokens are accepted from the Authorization header or the
    # sec-websocket-protocol channel only. Query parameters are rejected
    # because URLs are logged by proxies and access logs.
    token = ""
    authorization = websocket.headers.get("authorization", "")
    scheme, _, auth_token = authorization.partition(" ")
    if scheme.lower() == "bearer" and auth_token.strip():
        token = auth_token.strip()
    elif "sec-websocket-protocol" in websocket.headers:
        token = websocket.headers.get("sec-websocket-protocol", "").strip()

    authenticator: AdminAuthenticator = websocket.app.state.authenticator
    try:
        return authenticator.authenticate(token)
    except AuthenticationError as exc:
        logger.warning(
            "websocket_authentication_rejected",
            path=websocket.url.path,
            has_authorization_header=bool(authorization),
            used_subprotocol=(
                "sec-websocket-protocol" in websocket.headers and not authorization
            ),
            query_token_present="token" in websocket.query_params,
        )
        await websocket.close(code=1008, reason="invalid administrator credentials")
        raise exc
