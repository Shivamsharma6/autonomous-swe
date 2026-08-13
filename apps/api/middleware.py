from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from observability.tracing import CorrelationContext, bind_correlation, reset_correlation

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class CorrelationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, trust_internal_headers: bool = False) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._trust_internal_headers = trust_internal_headers

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        from uuid import uuid4

        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else str(uuid4())
        if self._trust_internal_headers:
            context = CorrelationContext.from_headers(dict(request.headers)).model_copy(
                update={"request_id": request_id}
            )
        else:
            context = CorrelationContext(request_id=request_id)
        token = bind_correlation(context)
        request.state.correlation = context
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_correlation(token)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, maximum_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._maximum_bytes = maximum_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self._maximum_bytes:
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
        body = await request.body()
        if len(body) > self._maximum_bytes:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, requests_per_minute: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "OPTIONS" or request.url.path.startswith("/health/"):
            return await call_next(request)
        host = request.client.host if request.client is not None else "unknown"
        credential_digest = hashlib.sha256(
            request.headers.get("authorization", "anonymous").encode("utf-8")
        ).hexdigest()[:16]
        key = f"{host}:{request.url.path}:{credential_digest}"
        now = time.monotonic()
        async with self._lock:
            entries = self._requests[key]
            while entries and entries[0] <= now - 60:
                entries.popleft()
            if len(entries) >= self._limit:
                return JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
            entries.append(now)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response
