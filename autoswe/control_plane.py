import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from autoswe.models import TaskNode, TaskStatus, ModelProviderConfig
from autoswe.scheduler import TaskScheduler
from autoswe.storage import StorageEngine

app = FastAPI(title="Autonomous Software Engineering Control Plane API")
storage = StorageEngine()
scheduler = TaskScheduler()

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
    active_provider_config = config
    return {"status": "updated", "config": active_provider_config.model_dump()}



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
def create_task(req: TaskCreateReq) -> Dict[str, Any]:
    task_id = req.task_id or f"task-{int(time.time() * 1000)}"
    try:
        task_dict = storage.create_task(
            task_id=task_id,
            project_id=req.project_id,
            title=req.user_request,
            description=req.description,
            status=TaskStatus.PENDING,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404, detail="Project not found")
    node = TaskNode(
        id=task_id,
        title=req.user_request,
        name=req.user_request,
        description=req.description,
        status=TaskStatus.PENDING,
    )
    scheduler.register_task(node)
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

