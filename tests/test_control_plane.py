import pytest
from fastapi.testclient import TestClient
from apps.api.main import app
from apps.api.routes import storage, manager


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def mock_background(task_id, project_id, user_request):
        pass

    monkeypatch.setattr("apps.api.routes._run_workflow_background", mock_background)

    db_path = str(tmp_path / "test_cp.db")
    artifact_dir = str(tmp_path / "artifacts")
    storage.db_path = db_path
    storage.storage_dir = artifact_dir
    storage.artifact_dir = artifact_dir
    storage._init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_api_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_project_creation(client):
    res_proj = client.post(
        "/api/v1/projects",
        json={"name": "Test Project", "repo_path": "/tmp/repo", "description": "Sample description"}
    )
    assert res_proj.status_code == 200
    data = res_proj.json()
    assert "project_id" in data
    assert data["name"] == "Test Project"
    assert data["project_id"].startswith("proj-")


def test_task_creation_retrieval_and_cancellation(client):
    res_proj = client.post(
        "/api/v1/projects",
        json={"name": "Demo App", "repo_path": "/tmp/demo"}
    )
    proj_id = res_proj.json()["project_id"]

    res_task = client.post(
        "/api/v1/tasks",
        json={"project_id": proj_id, "user_request": "Implement authentication endpoint"}
    )
    assert res_task.status_code == 200
    task_data = res_task.json()
    assert "task_id" in task_data
    task_id = task_data["task_id"]
    assert task_data["status"] == "PENDING"

    res_get = client.get(f"/api/v1/tasks/{task_id}")
    assert res_get.status_code == 200
    get_data = res_get.json()
    assert get_data["id"] == task_id
    assert get_data["project_id"] == proj_id

    res_cancel = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert res_cancel.status_code == 200
    cancel_data = res_cancel.json()
    assert cancel_data["task_id"] == task_id
    assert cancel_data["status"] == "CANCELLED"

    res_get_after = client.get(f"/api/v1/tasks/{task_id}")
    assert res_get_after.status_code == 200
    assert res_get_after.json()["status"].upper() == "CANCELLED"


def test_get_nonexistent_task(client):
    res = client.get("/api/v1/tasks/nonexistent-id-999")
    assert res.status_code == 404
    assert res.json()["detail"] == "Task not found"


def test_cancel_nonexistent_task(client):
    res = client.post("/api/v1/tasks/nonexistent-id-999/cancel")
    assert res.status_code == 404


def test_websocket_stream_endpoint(client):
    res_proj = client.post("/api/v1/projects", json={"name": "Stream Demo", "repo_path": "/tmp/stream"})
    proj_id = res_proj.json()["project_id"]
    res_task = client.post("/api/v1/tasks", json={"project_id": proj_id, "user_request": "Stream task progress"})
    task_id = res_task.json()["task_id"]

    with client.websocket_connect(f"/api/v1/tasks/{task_id}/stream") as websocket:
        initial_msg = websocket.receive_json()
        assert "task_id" in initial_msg
        assert initial_msg["task_id"] == task_id

        websocket.send_text("ping")
        reply = websocket.receive_json()
        assert "task_id" in reply
        assert reply["task_id"] == task_id


def test_create_task_invalid_project_id(client):
    res = client.post(
        "/api/v1/tasks",
        json={"project_id": "nonexistent-proj-id", "user_request": "Test request"}
    )
    assert res.status_code == 200
    assert "task_id" in res.json()


def test_websocket_disconnect_cleanup(client):
    res_proj = client.post("/api/v1/projects", json={"name": "Cleanup Demo", "repo_path": "/tmp/cleanup"})
    proj_id = res_proj.json()["project_id"]
    res_task = client.post("/api/v1/tasks", json={"project_id": proj_id, "user_request": "Cleanup task"})
    task_id = res_task.json()["task_id"]

    initial_connections = len(manager.active_connections)
    with client.websocket_connect(f"/api/v1/tasks/{task_id}/stream") as websocket:
        initial_msg = websocket.receive_json()
        assert initial_msg["task_id"] == task_id
        assert len(manager.active_connections) == initial_connections + 1

    assert len(manager.active_connections) == initial_connections


def test_broadcast_removes_failed_connection():
    import asyncio
    from apps.api.websocket import ConnectionManager

    class MockWS:
        async def send_json(self, msg):
            raise RuntimeError("Connection closed")

    cm = ConnectionManager()
    mock_ws = MockWS()
    cm.active_connections.append(mock_ws)
    assert mock_ws in cm.active_connections
    asyncio.run(cm.broadcast({"test": "data"}))
    assert mock_ws not in cm.active_connections


def test_provider_config_endpoints(client):
    res_get = client.get("/api/v1/provider-config")
    assert res_get.status_code == 200
    assert "provider" in res_get.json()

    new_config = {
        "provider": "custom",
        "model_name": "qwen2.5-coder",
        "base_url": "http://localhost:8080/v1",
        "api_key": "",
        "temperature": 0.2
    }
    res_post = client.post("/api/v1/provider-config", json=new_config)
    assert res_post.status_code == 200
    data = res_post.json()
    assert data["status"] == "updated"
    assert data["config"]["provider"] == "custom"
    assert data["config"]["base_url"] == "http://localhost:8080/v1"


def test_ollama_provider_config(client):
    ollama_config = {
        "provider": "ollama",
        "model_name": "qwen2.5-coder",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "temperature": 0.2
    }
    res_post = client.post("/api/v1/provider-config", json=ollama_config)
    assert res_post.status_code == 200
    data = res_post.json()
    assert data["status"] == "updated"
    assert data["config"]["provider"] == "ollama"
    assert data["config"]["base_url"] == "http://localhost:11434/v1"
