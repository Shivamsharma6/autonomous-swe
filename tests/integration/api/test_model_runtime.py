"""Outbound model requests from independent service instances use the launch snapshot."""

import json
from uuid import UUID

import httpx

from agents.gateway import ModelMessage, ModelRequest
from apps.api.main import create_app
from persistence.tables import RunRow
from tests.integration.api.test_control_plane import ADMIN_TOKEN, services


async def test_run_snapshot_controls_independent_service_model_requests(database, tmp_path):
    from agents.configuration import ModelRuntimeFactory

    configured = services(database, tmp_path)
    root = configured.settings.repository_import_root / "runtime-test"
    root.mkdir()
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured)),
        base_url="http://test", headers=headers,
    ) as client:
        project = (await client.post("/api/v1/projects", json={
            "name": "Runtime snapshot", "source_path": str(root),
        })).json()
        assert (await client.post("/api/v1/models/config", json={
            "base_url": "http://selected-provider.test/v1", "primary_model": "selected-model",
            "fallback_models": ["selected-backup"], "temperature": 0.35,
            "timeout_seconds": 187, "api_key": "private-test-key",
        })).status_code == 200
        launched = await client.post("/api/v1/runs", json={
            **project, "goal": "Use selected configuration", "baseline_commit": "a" * 40,
        })
        run_id = UUID(launched.json()["run_id"])
        assert (await client.post("/api/v1/models/config", json={
            "base_url": "http://different.test/v1", "primary_model": "different-model",
        })).status_code == 200
    async with database.sessions() as session:
        run = await session.get(RunRow, run_id)
        snapshot = run.model_configuration
    seen = []

    def provider(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{
                "id": "selected-model", "capabilities": ["structured_outputs", "tool_calling"],
            }]})
        seen.append(request)
        return httpx.Response(200, json={
            "model": json.loads(request.content)["model"],
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    # These factories represent separate dispatcher/worker processes. No shared Settings mutation.
    for _ in range(2):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as outbound:
            factory = ModelRuntimeFactory(configured.settings, client=outbound)
            resolved = factory.resolve(snapshot)
            assert resolved.primary_model == "selected-model"
            assert resolved.fallback_models == ("selected-backup",)
            await resolved.gateway.complete(ModelRequest(
                trace_id="snapshot-test", model=resolved.primary_model,
                messages=(ModelMessage(role="user", content="Return JSON"),),
                output_schema_name="result", output_schema={"type": "object"},
            ))
            await factory.close()
    for request in seen:
        assert str(request.url) == "http://selected-provider.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert request.extensions["timeout"]["read"] == 187
        assert json.loads(request.content)["temperature"] == 0.35
        assert json.loads(request.content)["model"] == "selected-model"
    assert len(seen) == 2
