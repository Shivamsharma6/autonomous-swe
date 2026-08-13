import asyncio
import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from agents.base import ModelProviderConfig
from knowledge.memory.storage import StorageEngine
from execution.scheduler.scheduler import TaskScheduler, TaskNode, TaskStatus
from apps.api.schemas import ProjectCreateReq, TaskCreateReq
from apps.api.websocket import manager

router = APIRouter(prefix="/api/v1")
storage = StorageEngine()
scheduler = TaskScheduler()

active_provider_config = ModelProviderConfig(
    provider="custom",
    model_name="nemotron-3.5-lightning:30b-mlx",
    base_url="",
    api_key="",
    temperature=0.2,
)


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

    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


async def _run_workflow_background(task_id: str, project_id: str, user_request: str):
    from workflows.feature import WorkflowOrchestrator
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


@router.get("/health")
def health_check() -> Dict[str, Any]:
    return {"status": "ok", "timestamp": time.time()}


@router.get("/provider-config")
def get_provider_config() -> Dict[str, Any]:
    return active_provider_config.model_dump()


@router.post("/provider-config")
def update_provider_config(config: ModelProviderConfig) -> Dict[str, Any]:
    global active_provider_config
    if not config.api_key:
        auto_key = _auto_detect_unsloth_key()
        if auto_key:
            config.api_key = auto_key
    active_provider_config = config
    return {"status": "updated", "config": active_provider_config.model_dump()}


@router.post("/provider-config/test")
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

    for base_url in candidates:
        models_url = f"{base_url}/models"
        req = urllib.request.Request(models_url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=1.0) as response:
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

    if config.provider == "ollama" or any("11434" in c for c in candidates):
        for base_url in candidates:
            tags_url = f"{base_url.replace('/v1', '').rstrip('/')}/api/tags"
            try:
                tags_req = urllib.request.Request(tags_url)
                with urllib.request.urlopen(tags_req, timeout=1.0) as response:
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


@router.post("/projects")
def create_project(req: ProjectCreateReq) -> Dict[str, Any]:
    project_id = req.project_id or f"proj-{int(time.time() * 1000)}"
    proj = storage.create_project(
        project_id=project_id,
        name=req.name,
        description=req.description,
        metadata={"repo_path": req.repo_path},
    )
    return {"project_id": proj["id"], "name": proj["name"], "status": "created"}


@router.post("/tasks")
async def create_task(req: TaskCreateReq) -> Dict[str, Any]:
    task_id = req.task_id or f"task-{int(time.time() * 1000)}"

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

    asyncio.create_task(_run_workflow_background(task_id, req.project_id, req.user_request))

    return {"task_id": task_dict["id"], "project_id": req.project_id, "status": "PENDING"}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> Dict[str, Any]:
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    storage.update_task_state(task_id=task_id, status=TaskStatus.CANCELLED)
    scheduler.cancel_task(task_id)
    return {"task_id": task_id, "status": "CANCELLED"}


@router.websocket("/tasks/{task_id}/stream")
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
