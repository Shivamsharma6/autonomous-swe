import asyncio
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from autoswe.models import TaskNode, TaskStatus, ModelProviderConfig
from autoswe.scheduler import TaskScheduler
from autoswe.storage import StorageEngine
from autoswe.logger import logger, log_event

app = FastAPI(title="Autonomous Software Engineering Control Plane API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage = StorageEngine()
scheduler = TaskScheduler()

def _auto_detect_unsloth_key() -> str:
    key_path = os.path.expanduser("~/.unsloth/studio/auth/agent_api_key.json")
    if os.path.exists(key_path):
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("servers", {})
            for server_url, s_data in servers.items():
                minted = s_data.get("minted", [])
                if minted:
                    return minted[0]
        except Exception:
            pass
    return ""

active_provider_config = ModelProviderConfig(
    provider="gemini",
    model_name="gemini-3.6-flash",
    base_url="",
    api_key="",
    temperature=0.2,
)



class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


class ProjectCreateReq(BaseModel):
    name: str
    repo_path: str = ""
    description: str = ""
    project_id: Optional[str] = None


class TaskCreateReq(BaseModel):
    project_id: str
    user_request: str
    description: str = ""
    task_id: Optional[str] = None
    model_provider: Optional[ModelProviderConfig] = None


@app.get("/api/v1/health")
def health_check() -> Dict[str, Any]:
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/v1/provider-config")
def get_provider_config() -> Dict[str, Any]:
    return active_provider_config.model_dump()


@app.post("/api/v1/provider-config")
def update_provider_config(config: ModelProviderConfig) -> Dict[str, Any]:
    global active_provider_config
    # If key is blank and provider is unsloth or custom, attempt auto-detection
    if not config.api_key:
        auto_key = _auto_detect_unsloth_key()
        if auto_key:
            config.api_key = auto_key
    active_provider_config = config
    return {"status": "updated", "config": active_provider_config.model_dump()}


def _resolve_url_candidates(base_url: str) -> List[str]:
    candidates = []
    if base_url:
        candidates.append(base_url)
        if "localhost" in base_url:
            candidates.append(base_url.replace("localhost", "host.docker.internal"))
        elif "127.0.0.1" in base_url:
            candidates.append(base_url.replace("127.0.0.1", "host.docker.internal"))
        elif "host.docker.internal" in base_url:
            candidates.append(base_url.replace("host.docker.internal", "localhost"))
    else:
        candidates = ["http://localhost:11434/v1", "http://host.docker.internal:11434/v1"]
    
    # Remove duplicates preserving order
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


@app.post("/api/v1/provider-config/test")
def test_provider_config(config: ModelProviderConfig) -> Dict[str, Any]:
    raw_base_url = config.base_url.rstrip("/")
    if not raw_base_url:
        if config.provider == "ollama":
            raw_base_url = "http://localhost:11434/v1"
        elif config.provider in ("custom", "unsloth", "local"):
            raw_base_url = "http://localhost:8888/v1"

    candidates = _resolve_url_candidates(raw_base_url)
    api_key = config.api_key or _auto_detect_unsloth_key()

    last_error = ""

    # Attempt 1: Standard OpenAI /v1/models endpoint across all candidates
    for base_url in candidates:
        models_url = f"{base_url}/models"
        req = urllib.request.Request(models_url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                models = []
                if isinstance(data, dict) and "data" in data:
                    models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
                return {
                    "success": True,
                    "status_code": response.status,
                    "base_url": base_url,
                    "api_key_detected": bool(api_key),
                    "api_key": api_key,
                    "available_models": models,
                    "message": f"Connected to {config.provider.upper()} server! Auto-detected models: {', '.join(models[:4]) or 'Loaded'}",
                }
        except Exception as e:
            last_error = str(e)

    # Attempt 2: Fallback to Ollama native /api/tags endpoint across candidates
    if config.provider == "ollama" or any("11434" in c for c in candidates):
        for base_url in candidates:
            tags_url = f"{base_url.replace('/v1', '').rstrip('/')}/api/tags"
            try:
                tags_req = urllib.request.Request(tags_url)
                with urllib.request.urlopen(tags_req, timeout=5) as response:
                    body = response.read().decode("utf-8")
                    data = json.loads(body)
                    models = []
                    if isinstance(data, dict) and "models" in data:
                        models = [m.get("name") for m in data["models"] if isinstance(m, dict) and m.get("name")]
                    return {
                        "success": True,
                        "status_code": response.status,
                        "base_url": base_url,
                        "api_key_detected": False,
                        "available_models": models,
                        "message": f"Connected to Ollama server at {tags_url}! Installed models ({len(models)}): {', '.join(models[:5])}",
                    }
            except Exception as e:
                last_error = str(e)

    return {
        "success": False,
        "status_code": 500,
        "base_url": raw_base_url,
        "api_key_detected": bool(api_key),
        "message": f"Connection failed to Ollama/LLM server: {last_error}",
    }




async def _run_workflow_background(task_id: str, project_id: str, user_request: str):
    from autoswe.orchestrator import WorkflowOrchestrator
    base_dir = os.path.abspath("autonomous_agent_directory")
    task_workspace = os.path.join(base_dir, task_id)
    os.makedirs(task_workspace, exist_ok=True)
    orchestrator = WorkflowOrchestrator(storage_engine=storage, workspace_path=task_workspace)
    loop = asyncio.get_running_loop()

    def progress_cb(event_type: str, message: str, payload: Any = None):
        msg = {
            "task_id": task_id,
            "event_type": event_type,
            "message": message,
            "payload": payload,
        }
        if isinstance(payload, dict):
            if "code_diff" in payload:
                msg["code_diff"] = payload["code_diff"]
            if "dag_nodes" in payload:
                msg["dag_nodes"] = payload["dag_nodes"]
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg), loop)

    await manager.broadcast({
        "task_id": task_id,
        "event_type": "SYSTEM",
        "message": f"Multi-agent SDLC workflow initialized for task: {user_request}",
        "payload": {"project_id": project_id, "provider": active_provider_config.provider, "model": active_provider_config.model_name}
    })

    res = await asyncio.to_thread(
        orchestrator.run_workflow,
        user_request=user_request,
        project_id=project_id,
        task_id=task_id,
        provider_config=active_provider_config,
        progress_callback=progress_cb,
    )
    
    await manager.broadcast({
        "task_id": task_id,
        "event_type": "SYSTEM",
        "message": f"Task execution completed with status: {res.get('workflow_status')}",
        "payload": res
    })


@app.post("/api/v1/projects")
def create_project(req: ProjectCreateReq) -> Dict[str, Any]:
    project_id = req.project_id or f"proj-{int(time.time() * 1000)}"
    proj = storage.create_project(
        project_id=project_id,
        name=req.name,
        description=req.description,
        metadata={"repo_path": req.repo_path},
    )
    return {"project_id": proj["id"], "name": proj["name"], "status": "created"}


@app.post("/api/v1/tasks")
async def create_task(req: TaskCreateReq) -> Dict[str, Any]:
    task_id = req.task_id or f"task-{int(time.time() * 1000)}"

    # Ensure project exists
    proj = storage.get_project(req.project_id)
    if not proj:
        storage.create_project(
            project_id=req.project_id,
            name="Default Project",
            description="Auto-created project",
        )

    task_dict = storage.create_task(
        task_id=task_id,
        project_id=req.project_id,
        title=req.user_request,
        description=req.description,
        status=TaskStatus.PENDING,
    )

    node = TaskNode(
        id=task_id,
        title=req.user_request,
        name=req.user_request,
        description=req.description,
        status=TaskStatus.PENDING,
    )
    scheduler.register_task(node)

    # Launch live background execution of agent workflow
    asyncio.create_task(_run_workflow_background(task_id, req.project_id, req.user_request))

    return {"task_id": task_dict["id"], "project_id": req.project_id, "status": "PENDING"}




@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str) -> Dict[str, Any]:
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    storage.update_task_state(task_id=task_id, status=TaskStatus.CANCELLED)
    scheduler.cancel_task(task_id)
    return {"task_id": task_id, "status": "CANCELLED"}


@app.websocket("/api/v1/tasks/{task_id}/stream")
async def websocket_stream(websocket: WebSocket, task_id: str) -> None:
    await manager.connect(websocket)
    try:
        task = await asyncio.to_thread(storage.get_task, task_id)
        await websocket.send_json({"task_id": task_id, "task": task, "timestamp": time.time()})
        while True:
            data = await websocket.receive_text()
            task = await asyncio.to_thread(storage.get_task, task_id)
            await websocket.send_json(
                {"task_id": task_id, "data": data, "task": task, "timestamp": time.time()}
            )
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)

