import contextlib
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import (
    ControlPlaneServices,
    get_services,
    require_admin,
    require_websocket_admin,
)
from apps.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    ArtifactMetadataResponse,
    AuditEventResponse,
    DeadLetterResponse,
    ModelConfigRequest,
    ModelConfigResponse,
    ModelProbeRequest,
    ModelProbeResponse,
    ModelTestRequest,
    ModelTestResponse,
    ProjectCreated,
    ProjectCreateRequest,
    ProjectOnboardRequest,
    ProjectOnboardResponse,
    RunCreated,
    RunCreateRequest,
    RunResponse,
    TaskResponse,
)
from apps.api.websocket import EventCursor, PostgresTaskEventSource
from observability.logging import get_structured_logger
from persistence.artifacts import ArtifactIntegrityError, ArtifactPathError
from persistence.tables import (
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    DeadLetterRow,
    ModelCallRow,
    PlanRevisionRow,
    RunRow,
    TaskRow,
    ToolExecutionRow,
    utc_now,
)
from tools.approval import ApprovalBindingError, ApprovalExpired

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_admin)],
)
Services = Annotated[ControlPlaneServices, Depends(get_services)]
DeadLetterLimit = Annotated[int, Query(ge=1, le=500)]
RunEventLimit = Annotated[int, Query(ge=1, le=1_000)]

logger = get_structured_logger("autoswe.api")
_GIT_EXECUTABLE = shutil.which("git")


def _api_key_preview(value: str) -> str:
    if len(value) >= 8:
        return f"{value[:4]}...{value[-4:]}"
    return "***" if value else ""


def _parses_as_json(value: str) -> bool:
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return False
    return True


def _run_git(
    target_dir: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    if _GIT_EXECUTABLE is None:
        raise RuntimeError("the git executable is not available")
    return subprocess.run(  # noqa: S603 - all values are policy-generated
        (_GIT_EXECUTABLE, *arguments),
        cwd=target_dir,
        check=False,
        capture_output=True,
        text=True,
    )


@router.get("/status")
async def status() -> dict[str, str]:
    return {"service": "autoswe-control-plane", "status": "ok"}


def _detect_provider(url: str) -> str:
    low = url.lower()
    if "openai.com" in low:
        return "OpenAI"
    if "openrouter.ai" in low:
        return "OpenRouter"
    if "anthropic.com" in low:
        return "Anthropic"
    if "deepseek.com" in low:
        return "DeepSeek"
    if "groq.com" in low:
        return "Groq"
    if "11434" in low or "ollama" in low:
        return "Ollama"
    if "together.xyz" in low or "together.ai" in low:
        return "Together AI"
    if "127.0.0.1" in low or "localhost" in low or "host.docker.internal" in low:
        return "Local Endpoint"
    return "Custom OpenAI"


def _normalize_backend_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.startswith("http://localhost:"):
        return cleaned.replace("http://localhost:", "http://host.docker.internal:")
    if cleaned.startswith("http://127.0.0.1:"):
        return cleaned.replace("http://127.0.0.1:", "http://host.docker.internal:")
    return cleaned


@router.get("/models/config", response_model=ModelConfigResponse)
async def get_model_config(services: Services) -> ModelConfigResponse:
    provider = _detect_provider(services.settings.model_base_url)
    api_key_str = services.settings.model_api_key.get_secret_value()
    return ModelConfigResponse(
        base_url=services.settings.model_base_url,
        primary_model=services.settings.model_primary,
        fallback_models=services.settings.model_fallbacks,
        timeout_seconds=services.settings.model_timeout_seconds,
        temperature=0.0,
        has_api_key=bool(api_key_str),
        api_key_preview=_api_key_preview(api_key_str),
        provider_name=provider,
    )


@router.post("/models/config", response_model=ModelConfigResponse)
async def update_model_config(
    request: ModelConfigRequest,
    services: Services,
) -> ModelConfigResponse:
    services.settings.model_base_url = _normalize_backend_url(request.base_url)
    services.settings.model_primary = request.primary_model
    services.settings.model_fallbacks = request.fallback_models
    services.settings.model_timeout_seconds = request.timeout_seconds
    if request.api_key:
        services.settings.model_api_key = SecretStr(request.api_key)
    provider = _detect_provider(services.settings.model_base_url)
    api_key_str = services.settings.model_api_key.get_secret_value()
    return ModelConfigResponse(
        base_url=services.settings.model_base_url,
        primary_model=services.settings.model_primary,
        fallback_models=services.settings.model_fallbacks,
        timeout_seconds=services.settings.model_timeout_seconds,
        temperature=request.temperature,
        has_api_key=bool(api_key_str),
        api_key_preview=_api_key_preview(api_key_str),
        provider_name=provider,
    )


@router.post("/models/probe", response_model=ModelProbeResponse)
async def probe_models(request: ModelProbeRequest) -> ModelProbeResponse:
    base_url = _normalize_backend_url(request.base_url)
    headers = {"Authorization": f"Bearer {request.api_key}"} if request.api_key else {}
    start_t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            endpoints_to_try = [
                f"{base_url}/models",
                f"{base_url.removesuffix('/v1')}/api/tags",
            ]
            resp = None
            for ep in endpoints_to_try:
                try:
                    r = await client.get(ep, headers=headers)
                except Exception as error:
                    logger.debug(
                        "model_probe_endpoint_failed",
                        endpoint=ep,
                        error_type=type(error).__name__,
                    )
                    continue
                if r.status_code < 400:
                    resp = r
                    break

            latency = (time.perf_counter() - start_t) * 1000.0
            if resp is not None and resp.status_code < 400:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                model_ids: list[str] = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "id" in item:
                            model_ids.append(str(item["id"]))
                elif (
                    isinstance(body, dict)
                    and "models" in body
                    and isinstance(body["models"], list)
                ):
                    for item in body["models"]:
                        if isinstance(item, dict) and "name" in item:
                            model_ids.append(str(item["name"]))
                        elif isinstance(item, str):
                            model_ids.append(item)
                return ModelProbeResponse(
                    reachable=True,
                    models=model_ids or ["default"],
                    latency_ms=round(latency, 1),
                    error=None,
                )
            else:
                detail = resp.status_code if resp else "Connection Failed"
                return ModelProbeResponse(
                    reachable=False,
                    models=[],
                    latency_ms=round(latency, 1),
                    error=f"Endpoint returned HTTP {detail}",
                )
    except Exception as exc:
        latency = (time.perf_counter() - start_t) * 1000.0
        return ModelProbeResponse(
            reachable=False,
            models=[],
            latency_ms=round(latency, 1),
            error=str(exc),
        )


@router.post("/models/test", response_model=ModelTestResponse)
async def test_model(request: ModelTestRequest) -> ModelTestResponse:
    base_url = _normalize_backend_url(request.base_url)
    headers = {"Authorization": f"Bearer {request.api_key}"} if request.api_key else {}
    start_t = time.perf_counter()
    payload = {
        "model": request.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    'You are a test probe. Respond with strictly valid JSON only: '
                    '{"status": "ok", "verified": true}'
                ),
            },
            {"role": "user", "content": "Verify system connectivity and JSON generation."},
        ],
        "temperature": 0.0,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            latency = (time.perf_counter() - start_t) * 1000.0
            if resp.status_code < 400:
                body = resp.json()
                choices = body.get("choices", [])
                content = choices[0]["message"]["content"] if choices else ""
                is_json = _parses_as_json(content)
                return ModelTestResponse(
                    success=True,
                    model=request.model,
                    latency_ms=round(latency, 1),
                    structured_output=is_json,
                    response_snippet=content[:300],
                    error=None,
                )
            else:
                return ModelTestResponse(
                    success=False,
                    model=request.model,
                    latency_ms=round(latency, 1),
                    structured_output=False,
                    response_snippet="",
                    error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                )
    except Exception as exc:
        latency = (time.perf_counter() - start_t) * 1000.0
        return ModelTestResponse(
            success=False,
            model=request.model,
            latency_ms=round(latency, 1),
            structured_output=False,
            response_snippet="",
            error=str(exc),
        )


@router.post("/projects/onboard", response_model=ProjectOnboardResponse, status_code=201)
async def onboard_project(
    request: ProjectOnboardRequest,
    services: Services,
) -> ProjectOnboardResponse:
    import_root = services.settings.repository_import_root.resolve(strict=False)
    import_root.mkdir(parents=True, exist_ok=True)

    folder_candidate = (request.folder_name or request.source_path or request.name).strip()
    safe_folder = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", folder_candidate.split("/")[-1]) or "project"
    target_dir = (import_root / safe_folder).resolve()

    if not str(target_dir).startswith(str(import_root)):
        target_dir = import_root / safe_folder

    target_dir.mkdir(parents=True, exist_ok=True)

    for file_info in request.files:
        clean_rel = re.sub(r"^[/\\]+", "", file_info.path)
        if ".." in clean_rel.split("/"):
            continue
        dest_path = (target_dir / clean_rel).resolve()
        if not str(dest_path).startswith(str(target_dir)):
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(file_info.content, encoding="utf-8", errors="replace")

    baseline_sha = _ensure_git_repository(target_dir, request.default_branch)

    async with services.database.transaction() as session:
        await services.database_repository.create_project(
            session,
            project_id=request.project_id,
            name=request.name,
        )
        await services.database_repository.create_repository(
            session,
            repository_id=request.repository_id,
            project_id=request.project_id,
            source_path=str(target_dir),
            default_branch=request.default_branch,
        )

    return ProjectOnboardResponse(
        project_id=request.project_id,
        repository_id=request.repository_id,
        name=request.name,
        source_path=str(target_dir),
        default_branch=request.default_branch,
        baseline_commit=baseline_sha,
    )


@router.post("/projects", response_model=ProjectCreated, status_code=201)
async def create_project(
    request: ProjectCreateRequest,
    services: Services,
) -> ProjectCreated:
    source_path = _validated_repository_source(
        services.settings.repository_import_root,
        request.source_path,
    )
    async with services.database.transaction() as session:
        await services.database_repository.create_project(
            session,
            project_id=request.project_id,
            name=request.name,
        )
        await services.database_repository.create_repository(
            session,
            repository_id=request.repository_id,
            project_id=request.project_id,
            source_path=str(source_path),
            default_branch=request.default_branch,
        )
    return ProjectCreated(
        project_id=request.project_id,
        repository_id=request.repository_id,
    )


@router.post("/runs", response_model=RunCreated, status_code=202)
async def create_run(
    request: RunCreateRequest,
    services: Services,
) -> RunCreated:
    event_id = uuid4()
    async with services.database.transaction() as session:
        try:
            await services.database_repository.create_run(
                session,
                run_id=request.run_id,
                project_id=request.project_id,
                repository_id=request.repository_id,
                goal=request.goal,
                baseline_commit=request.baseline_commit,
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=404, detail="project or repository not found") from exc
        payload = {
            "run_id": str(request.run_id),
            "project_id": str(request.project_id),
            "repository_id": str(request.repository_id),
        }
        await services.database_repository.append_audit(
            session,
            event_id=event_id,
            event_type="run.requested",
            aggregate_type="run",
            aggregate_id=request.run_id,
            payload=payload,
            correlation_id=request.run_id,
            causation_id=event_id,
        )
        await services.database_repository.enqueue_event(
            session,
            event_id=event_id,
            topic="run-requests",
            payload=payload,
        )
    return RunCreated(run_id=request.run_id, state="PENDING")


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, services: Services) -> RunResponse:
    async with services.database.sessions() as session:
        run = await session.get(RunRow, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        active_revision = await session.scalar(
            select(func.max(PlanRevisionRow.revision)).where(PlanRevisionRow.run_id == run_id)
        )
        counts = tuple(
            (
                await session.execute(
                    select(TaskRow.state, func.count())
                    .where(TaskRow.run_id == run_id)
                    .group_by(TaskRow.state)
                )
            ).all()
        )
        usage = (
            await session.execute(
                select(
                    func.coalesce(func.sum(ModelCallRow.input_tokens), 0),
                    func.coalesce(func.sum(ModelCallRow.output_tokens), 0),
                    func.coalesce(func.sum(ModelCallRow.cost_usd), 0.0),
                ).where(ModelCallRow.run_id == run_id)
            )
        ).one()
    now = utc_now()
    return RunResponse(
        run_id=run.id,
        project_id=run.project_id,
        repository_id=run.repository_id,
        goal=run.goal,
        baseline_commit=run.baseline_commit,
        state=run.state,
        state_entered_at=run.state_entered_at.isoformat(),
        state_duration_seconds=max(0.0, (now - run.state_entered_at).total_seconds()),
        active_plan_revision=active_revision,
        task_counts={state.value: int(count) for state, count in counts},
        model_input_tokens=int(usage[0]),
        model_output_tokens=int(usage[1]),
        model_cost_usd=float(usage[2]),
        created_at=run.created_at.isoformat(),
        updated_at=run.updated_at.isoformat(),
    )


@router.get("/runs/{run_id}/tasks", response_model=tuple[TaskResponse, ...])
async def list_run_tasks(run_id: UUID, services: Services) -> tuple[TaskResponse, ...]:
    async with services.database.sessions() as session:
        await _require_run(session, run_id)
        rows = tuple(
            (
                await session.scalars(
                    select(TaskRow)
                    .where(TaskRow.run_id == run_id)
                    .order_by(TaskRow.plan_revision, TaskRow.created_at, TaskRow.id)
                )
            ).all()
        )
    return tuple(_task_response(row) for row in rows)


@router.get("/runs/{run_id}/approvals", response_model=tuple[ApprovalResponse, ...])
async def list_run_approvals(
    run_id: UUID, services: Services
) -> tuple[ApprovalResponse, ...]:
    async with services.database.sessions() as session:
        await _require_run(session, run_id)
        rows = tuple(
            (
                await session.execute(
                    select(ApprovalRow, ToolExecutionRow)
                    .join(ToolExecutionRow, ToolExecutionRow.id == ApprovalRow.call_id)
                    .where(ToolExecutionRow.run_id == run_id)
                    .order_by(ApprovalRow.created_at, ApprovalRow.id)
                )
            ).all()
        )
    return tuple(
        ApprovalResponse(
            approval_id=approval.id,
            call_id=approval.call_id,
            status=approval.status.value,
            call_hash=approval.call_hash,
            tool_name=execution.tool_name,
            requested_by=execution.requested_by,
            arguments=execution.arguments,
            expires_at=approval.expires_at.isoformat(),
            created_at=approval.created_at.isoformat(),
            decided_at=approval.decided_at.isoformat() if approval.decided_at else None,
            approver=approval.approver,
        )
        for approval, execution in rows
    )


@router.get("/runs/{run_id}/artifacts", response_model=tuple[ArtifactMetadataResponse, ...])
async def list_run_artifacts(
    run_id: UUID, services: Services
) -> tuple[ArtifactMetadataResponse, ...]:
    async with services.database.sessions() as session:
        await _require_run(session, run_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.run_id == run_id)
                    .order_by(ArtifactRow.created_at, ArtifactRow.id)
                )
            ).all()
        )
    return tuple(
        ArtifactMetadataResponse(
            artifact_id=row.id,
            task_id=row.task_id,
            sha256=row.sha256,
            media_type=row.media_type,
            state=row.state.value,
            size_bytes=row.size_bytes,
            verified_at=row.verified_at.isoformat() if row.verified_at else None,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    )


@router.get("/runs/{run_id}/events", response_model=tuple[AuditEventResponse, ...])
async def list_run_events(
    run_id: UUID,
    services: Services,
    limit: RunEventLimit = 500,
) -> tuple[AuditEventResponse, ...]:
    async with services.database.sessions() as session:
        await _require_run(session, run_id)
        rows = tuple(
            (
                await session.scalars(
                    select(AuditEventRow)
                    .where(AuditEventRow.correlation_id == run_id)
                    .order_by(AuditEventRow.created_at, AuditEventRow.id)
                    .limit(limit)
                )
            ).all()
        )
    return tuple(
        AuditEventResponse(
            event_id=row.id,
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            payload=row.payload,
            content_hash=row.content_hash,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    )


@router.get("/projects/{project_id}/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    project_id: UUID,
    task_id: UUID,
    services: Services,
) -> TaskResponse:
    async with services.database.sessions() as session:
        row = await services.database_repository.get_task(
            session,
            project_id=project_id,
            task_id=task_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_response(row)


@router.post("/projects/{project_id}/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(
    project_id: UUID,
    task_id: UUID,
    services: Services,
) -> dict[str, str]:
    try:
        await services.scheduler.cancel_task(
            project_id=project_id,
            task_id=task_id,
            notify=services.cancel_notify,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return {"task_id": str(task_id), "status": "CANCELLED"}


@router.post("/approvals/{approval_id}/decision", status_code=202)
async def decide_approval(
    approval_id: UUID,
    decision: ApprovalDecisionRequest,
    services: Services,
) -> dict[str, str]:
    try:
        await services.approvals.decide(
            approval_id,
            approver=decision.approver,
            approved=decision.approved,
            expected_call_hash=decision.expected_call_hash,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc
    except ApprovalExpired as exc:
        raise HTTPException(status_code=409, detail="approval expired") from exc
    except ApprovalBindingError as exc:
        raise HTTPException(status_code=409, detail="approval binding mismatch") from exc
    return {
        "approval_id": str(approval_id),
        "status": "APPROVED" if decision.approved else "REJECTED",
    }


@router.get("/projects/{project_id}/artifacts/{artifact_id}")
async def download_artifact(
    project_id: UUID,
    artifact_id: UUID,
    services: Services,
) -> Response:
    try:
        async with services.database.transaction() as session:
            row = await services.database_repository.get_artifact(
                session,
                project_id=project_id,
                artifact_id=artifact_id,
            )
            if row is None:
                raise LookupError
            content = await services.artifacts.get_verified(
                session,
                project_id=project_id,
                artifact_id=artifact_id,
            )
            media_type = row.media_type
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    except (ArtifactIntegrityError, ArtifactPathError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=409,
            detail="artifact failed integrity verification",
        ) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact_id}"'},
    )


@router.get("/dead-letters", response_model=tuple[DeadLetterResponse, ...])
async def list_dead_letters(
    services: Services,
    include_resolved: bool = False,
    limit: DeadLetterLimit = 100,
) -> tuple[DeadLetterResponse, ...]:
    async with services.database.sessions() as session:
        statement = select(DeadLetterRow).order_by(
            DeadLetterRow.created_at.desc(), DeadLetterRow.id
        )
        if not include_resolved:
            statement = statement.where(DeadLetterRow.resolved_at.is_(None))
        rows = tuple((await session.scalars(statement.limit(limit))).all())
    return tuple(
        DeadLetterResponse(
            dead_letter_id=row.id,
            event_id=row.event_id,
            consumer=row.consumer,
            topic=row.topic,
            attempts=row.attempts,
            last_error=_safe_delivery_error(row.last_error),
            created_at=row.created_at.isoformat(),
            resolved=row.resolved_at is not None,
        )
        for row in rows
    )


@router.post("/dead-letters/{dead_letter_id}/replay", status_code=202)
async def replay_dead_letter(
    dead_letter_id: UUID,
    services: Services,
) -> dict[str, str]:
    async with services.database.transaction() as session:
        row = await session.scalar(
            select(DeadLetterRow)
            .where(DeadLetterRow.id == dead_letter_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="dead letter not found")
        if row.resolved_at is None:
            row.resolved_at = utc_now()
            await services.database_repository.enqueue_event(
                session,
                event_id=uuid4(),
                topic=row.topic,
                payload={**row.payload, "replayed_dead_letter_id": str(row.id)},
            )
    return {"dead_letter_id": str(dead_letter_id), "status": "REQUEUED"}


@router.websocket("/projects/{project_id}/tasks/{task_id}/events")
async def task_events(websocket: WebSocket, project_id: UUID, task_id: UUID) -> None:
    try:
        await require_websocket_admin(websocket)
    except Exception:
        return
    services: ControlPlaneServices = websocket.app.state.services
    async with services.database.sessions() as session:
        task = await services.database_repository.get_task(
            session,
            project_id=project_id,
            task_id=task_id,
        )
    if task is None:
        await websocket.close(code=1008, reason="task not found")
        return
    await websocket.accept()
    source = PostgresTaskEventSource(services.database)
    cursor: EventCursor | None = None
    try:
        while True:
            events = await source.next_events(task_id, after=cursor)
            for event in events:
                await websocket.send_json(event)
                cursor = EventCursor(
                    created_at=datetime.fromisoformat(str(event["created_at"])),
                    event_id=UUID(str(event["event_id"])),
                )
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close()


def _task_response(row: TaskRow) -> TaskResponse:
    return TaskResponse(
        task_id=row.id,
        run_id=row.run_id,
        project_id=row.project_id,
        repository_id=row.repository_id,
        state=row.state.value,
        version=row.version,
        task_type=row.task_type.value,
        title=row.title,
        state_entered_at=row.state_entered_at.isoformat(),
        plan_revision=row.plan_revision,
        dependencies=tuple(UUID(value) for value in row.dependencies),
        assigned_capability=row.assigned_capability,
        acceptance_criteria=tuple(row.acceptance_criteria),
        allowed_tools=tuple(row.allowed_tools),
        risk_ceiling=row.risk_ceiling,
    )


async def _require_run(session: AsyncSession, run_id: UUID) -> RunRow:
    row = await session.get(RunRow, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


def _ensure_git_repository(target_dir: Path, default_branch: str = "main") -> str:
    target_dir.mkdir(parents=True, exist_ok=True)
    git_dir = target_dir / ".git"
    if not git_dir.exists():
        _run_git(target_dir, "init", "-b", default_branch)
        _run_git(target_dir, "config", "user.name", "AutoSWE System")
        _run_git(target_dir, "config", "user.email", "autoswe@local")

    pyproject = target_dir / "pyproject.toml"
    package_json = target_dir / "package.json"
    if not pyproject.exists() and not package_json.exists():
        pyproject.write_text(
            '[project]\nname = "service"\nversion = "0.1.0"\n'
            'dependencies = []\n\n'
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8",
        )
        (target_dir / "requirements.txt").write_text("# requirements\n", encoding="utf-8")
        src_dir = target_dir / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / "__init__.py").write_text("", encoding="utf-8")
        (src_dir / "service.py").write_text(
            'def handle_request(payload: dict) -> dict:\n'
            '    return {"status": "ok", "payload": payload}\n',
            encoding="utf-8",
        )
        tests_dir = target_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_service.py").write_text(
            "from src.service import handle_request\n\n"
            "def test_handle_request():\n"
            '    assert handle_request({})["status"] == "ok"\n',
            encoding="utf-8",
        )

    res = _run_git(target_dir, "rev-parse", "HEAD")
    rev = res.stdout.strip()
    if not rev or res.returncode != 0:
        _run_git(target_dir, "add", ".")
        _run_git(target_dir, "commit", "-m", "Initial baseline commit", "--allow-empty")
        res = _run_git(target_dir, "rev-parse", "HEAD")
        rev = res.stdout.strip()
    return rev


def _validated_repository_source(import_root: Path, requested: str) -> Path:
    try:
        root = import_root.resolve(strict=True)
        req_path = Path(requested)
        candidate = (
            root / req_path if not req_path.is_absolute() else req_path
        ).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="repository source must be an existing path inside the import root",
        ) from exc
    if candidate.is_symlink() or not candidate.is_dir():
        raise HTTPException(status_code=422, detail="repository source must be a directory")
    return candidate


def _safe_delivery_error(value: str) -> str:
    lowered = value.casefold()
    sensitive_markers = ("bearer ", "api_key", "token=", "password=", "secret=")
    if any(marker in lowered for marker in sensitive_markers):
        return "delivery failed; sensitive details redacted"
    return value[:1_000]
