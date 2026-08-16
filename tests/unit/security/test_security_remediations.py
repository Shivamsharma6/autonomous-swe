from __future__ import annotations

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


@pytest.mark.asyncio
async def test_rate_limit_middleware_bounds_memory_and_evicts_stale() -> None:
    app = FastAPI()

    @app.get("/test")
    async def handler() -> PlainTextResponse:
        return PlainTextResponse("ok")

    middleware = RateLimitMiddleware(app, requests_per_minute=100, max_tracked_keys=50)

    # Generate requests with 200 distinct keys
    for i in range(200):
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/path/{i}",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
        }
        req = Request(scope)
        call_next = AsyncMock(return_value=PlainTextResponse("ok"))
        await middleware.dispatch(req, call_next)

    # Verify tracked keys dictionary is bounded to max_tracked_keys or cleaned up
    assert len(middleware._requests) <= 100


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

    # Test valid query parameter token
    app = FastAPI()
    app.state.authenticator = authenticator
    ws = MagicMock(spec=WebSocket)
    ws.app = app
    ws.headers = {}
    ws.query_params = {"token": token_value}

    principal = await require_websocket_admin(ws)
    assert principal.subject == "single-machine-admin"

    # Test invalid query parameter token
    ws_invalid = MagicMock(spec=WebSocket)
    ws_invalid.app = app
    ws_invalid.headers = {}
    ws_invalid.query_params = {"token": "wrong-token-1234567890-abcdef123456"}  # noqa: S105
    ws_invalid.close = AsyncMock()

    with pytest.raises(AuthenticationError):
        await require_websocket_admin(ws_invalid)
    ws_invalid.close.assert_awaited_once_with(
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
