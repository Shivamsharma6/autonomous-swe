from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI, WebSocket
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from apps.api.auth import AdminAuthenticator, AuthenticationError
from apps.api.dependencies import require_admin, require_websocket_admin
from apps.api.middleware import RateLimitMiddleware
from domain.enums import RiskLevel
from policies.guardrails.secret_redactor import SecretRedactor, is_sensitive_key
from policies.risk.policy_engine import ToolRiskPolicy


def _request(path: str, *, token: bytes = b"credential") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"testserver"), (b"authorization", token)],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_rate_limit_blocks_a_single_credential_across_many_paths() -> None:
    """The limiter must be a per-client budget, not a per-route quota.

    The previous implementation keyed on `request.url.path`, so one credential
    received a fresh budget for every endpoint it touched and was effectively
    never throttled. This drives the same credential across distinct paths and
    asserts the budget is shared.
    """
    app = FastAPI()
    middleware = RateLimitMiddleware(app, requests_per_minute=10, max_tracked_keys=100)
    call_next = AsyncMock(return_value=PlainTextResponse("ok"))

    statuses = []
    for index in range(25):
        response = await middleware.dispatch(_request(f"/path/{index}"), call_next)
        statuses.append(response.status_code)

    assert statuses.count(429) == 15
    assert statuses[:10] == [200] * 10


@pytest.mark.asyncio
async def test_distinct_credentials_have_independent_budgets() -> None:
    app = FastAPI()
    middleware = RateLimitMiddleware(app, requests_per_minute=3, max_tracked_keys=100)
    call_next = AsyncMock(return_value=PlainTextResponse("ok"))

    for _ in range(3):
        first = await middleware.dispatch(_request("/x", token=b"token-a"), call_next)
        second = await middleware.dispatch(_request("/x", token=b"token-b"), call_next)
        assert first.status_code == 200, "token A must not be charged for token B"
        assert second.status_code == 200, "token B must not be charged for token A"

    assert (
        await middleware.dispatch(_request("/x", token=b"token-a"), call_next)
    ).status_code == 429
    assert (
        await middleware.dispatch(_request("/x", token=b"token-b"), call_next)
    ).status_code == 429


@pytest.mark.asyncio
async def test_capacity_refuses_rather_than_evicting_a_victims_budget() -> None:
    """Eviction let an unauthenticated caller reset any budget by filling the table.

    The limiter runs before authentication, so filling `max_tracked_keys` needed
    no credential and dropped the oldest bucket, restarting a live client from
    zero. At capacity with nothing idle the limiter now refuses.
    """
    app = FastAPI()
    middleware = RateLimitMiddleware(app, requests_per_minute=100, max_tracked_keys=5)
    call_next = AsyncMock(return_value=PlainTextResponse("ok"))

    victim = await middleware.dispatch(_request("/victim", token=b"victim"), call_next)
    assert victim.status_code == 200
    victim_key = f"127.0.0.1:{hashlib.sha256(b'victim').hexdigest()[:16]}"
    budget_before = len(middleware._requests[victim_key])

    for index in range(20):
        await middleware.dispatch(
            _request(f"/flood/{index}", token=f"flood-{index}".encode()), call_next
        )

    victim_key = f"127.0.0.1:{hashlib.sha256(b'victim').hexdigest()[:16]}"
    assert victim_key in middleware._requests, "a live budget must not be evicted"
    assert len(middleware._requests[victim_key]) == budget_before


@pytest.mark.asyncio
async def test_tracked_buckets_stay_bounded() -> None:
    app = FastAPI()
    middleware = RateLimitMiddleware(app, requests_per_minute=1000, max_tracked_keys=50)
    call_next = AsyncMock(return_value=PlainTextResponse("ok"))

    for index in range(500):
        await middleware.dispatch(_request(f"/p/{index}", token=f"t{index}".encode()), call_next)

    assert len(middleware._requests) <= 50


def test_tool_risk_policy_nested_and_traversal_paths() -> None:
    policy = ToolRiskPolicy(
        protected_paths=(".github/workflows", "infrastructure", ".git", "terraform"),
        repository_floor=RiskLevel.LOW,
    )

    # Direct match
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="write_file",
            arguments={"path": ".github/workflows/deploy.yml"},
            side_effect="local",
        )
        is RiskLevel.HIGH
    )

    # Nested path
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="read_file",
            arguments={"path": "src/infrastructure/config.py"},
            side_effect="none",
        )
        is RiskLevel.HIGH
    )

    # Traversal path
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="write_file",
            arguments={"path": "subdir/../.github/workflows/ci.yml"},
            side_effect="local",
        )
        is RiskLevel.HIGH
    )

    # .git directory
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="read_file",
            arguments={"path": ".git/config"},
            side_effect="none",
        )
        is RiskLevel.HIGH
    )

    # Safe path
    assert (
        policy.calculate(
            base=RiskLevel.LOW,
            tool_name="read_file",
            arguments={"path": "src/components/button.tsx"},
            side_effect="none",
        )
        is RiskLevel.LOW
    )


def test_secret_redactor_expanded_patterns() -> None:
    redactor = SecretRedactor()

    # JWT Token
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgN_p_placeholder_signature_12345"
    )
    assert redactor.redact(f"Bearer {jwt}") == "[REDACTED]"

    # URL query parameter
    url_with_key = "https://api.example.com/data?api_key=super_secret_token_12345&foo=bar"
    redacted_url = redactor.redact(url_with_key)
    assert "super_secret_token_12345" not in redacted_url
    assert "[REDACTED]" in redacted_url

    # Sensitive keys
    assert is_sensitive_key("jwt") is True
    assert is_sensitive_key("x-api-key") is True
    assert is_sensitive_key("admin_token") is True
    assert is_sensitive_key("session_token") is True


@pytest.mark.asyncio
async def test_require_websocket_admin_query_param_token() -> None:
    token_value = "secret-admin-token-1234567890-abcdef123456"  # noqa: S105
    authenticator = AdminAuthenticator(token_value)

    # Query-parameter tokens are rejected: URLs are logged by proxies and
    # access logs, so they must never carry credentials.
    app = FastAPI()
    app.state.authenticator = authenticator
    ws = MagicMock(spec=WebSocket)
    ws.app = app
    ws.headers = {}
    ws.query_params = {"token": token_value}
    ws.url.path = "/api/v1/ws"
    ws.close = AsyncMock()

    with pytest.raises(AuthenticationError):
        await require_websocket_admin(ws)
    ws.close.assert_awaited_once_with(
        code=1008, reason="invalid administrator credentials"
    )


@pytest.mark.asyncio
async def test_sandbox_manager_requires_admin_token() -> None:
    app = FastAPI()
    admin_token = "secret-admin-token-1234567890-abcdef123456"  # noqa: S105
    app.state.authenticator = AdminAuthenticator(admin_token)

    @app.post("/executions", dependencies=[Depends(require_admin)])
    async def execute() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Unauthenticated request
        resp_unauth = await client.post("/executions")
        assert resp_unauth.status_code == 401

        # Invalid token request
        resp_invalid = await client.post(
            "/executions",
            headers={"Authorization": "Bearer wrong-token-1234567890-abcdef123456"},
        )
        assert resp_invalid.status_code == 401

        # Valid token request
        resp_valid = await client.post(
            "/executions",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_valid.status_code == 200
        assert resp_valid.json() == {"status": "ok"}
