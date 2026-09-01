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
    HistoryResponse,
    HistorySample,
    MessageResponse,
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
from domain.enums import RunStatus
from execution.scheduler.service import RUN_TERMINAL_VALUES
from observability.logging import get_structured_logger
from persistence.artifacts import ArtifactIntegrityError, ArtifactPathError
from persistence.model_settings import ModelConfiguration
from persistence.tables import (
    AgentMessageRow,
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    DeadLetterRow,
    ModelCallRow,
    PlanRevisionRow,
    ProjectRow,
    RunRow,
    RunStageAttemptRow,
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
    return _model_config_response(await services.model_settings.load())


def _model_config_response(configuration: ModelConfiguration) -> ModelConfigResponse:
    api_key_str = configuration.api_key.get_secret_value()
    return ModelConfigResponse(
        base_url=configuration.base_url,
        primary_model=configuration.primary_model,
        fallback_models=list(configuration.fallback_models),
        timeout_seconds=configuration.timeout_seconds,
        temperature=configuration.temperature,
        has_api_key=bool(api_key_str),
        api_key_preview=_api_key_preview(api_key_str),
        provider_name=_detect_provider(configuration.base_url),
    )


@router.post("/models/config", response_model=ModelConfigResponse)
async def update_model_config(
    request: ModelConfigRequest,
    services: Services,
) -> ModelConfigResponse:
    endpoint = _normalize_backend_url(request.base_url)
    try:
        url = httpx.URL(endpoint)
        if (
            url.scheme not in {"http", "https"} or not url.host
            or url.userinfo or url.query or url.fragment
        ):
            raise ValueError("invalid endpoint")
    except (httpx.InvalidURL, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail=(
                "Enter a valid HTTP or HTTPS provider base URL "
                "without credentials, query or fragment."
            ),
        ) from error
    previous = await services.model_settings.load()
    key = request.api_key
    if not key and endpoint == _normalize_backend_url(previous.base_url):
        key = previous.api_key.get_secret_value()
    configuration = ModelConfiguration(
        base_url=endpoint,
        api_key=SecretStr(key),
        primary_model=request.primary_model,
        fallback_models=tuple(request.fallback_models),
        timeout_seconds=request.timeout_seconds,
        temperature=request.temperature,
    )
    await services.model_settings.save(configuration)
    return _model_config_response(configuration)


async def _model_check_headers(base_url: str, api_key: str, services: Services) -> dict[str, str]:
    saved = await services.model_settings.load()
    if not api_key and base_url == _normalize_backend_url(saved.base_url):
        api_key = saved.api_key.get_secret_value()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


@router.post("/models/probe", response_model=ModelProbeResponse)
async def probe_models(request: ModelProbeRequest, services: Services) -> ModelProbeResponse:
    base_url = _normalize_backend_url(request.base_url)
    headers = await _model_check_headers(base_url, request.api_key, services)
    start_t = time.perf_counter()
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Accept both OpenAI-compatible model lists and Ollama's native tags API.
        for endpoint in [f"{base_url}/models", f"{base_url.removesuffix('/v1')}/api/tags"]:
            try:
                response = await client.get(endpoint, headers=headers)
            except httpx.InvalidURL:
                errors.append("Invalid provider URL. Enter a valid HTTP or HTTPS base URL.")
                break
            except httpx.TimeoutException:
                errors.append("Connection timed out. Check that the model server is running.")
                continue
            except httpx.RequestError as exc:
                errors.append(f"Could not connect to the model server ({type(exc).__name__}).")
                continue
            if response.status_code in (401, 403):
                errors.append(
                    f"HTTP {response.status_code}: check the provider API key and access."
                )
                break
            if not response.is_success:
                errors.append(f"Model list returned HTTP {response.status_code}.")
                continue
            try:
                body = response.json()
            except ValueError:
                errors.append("The endpoint did not return JSON. Check the provider base URL.")
                continue
            items = body.get("data", body.get("models")) if isinstance(body, dict) else None
            if not isinstance(items, list):
                errors.append(
                    "The endpoint did not return a model list. Check the provider base URL."
                )
                continue
            models = []
            for item in items:
                name = (
                    item.get("id", item.get("name", item.get("model")))
                    if isinstance(item, dict) else item
                )
                if isinstance(name, str) and name.strip() and name.strip() not in models:
                    models.append(name.strip())
            if items and not models:
                errors.append("The endpoint returned a model list without valid model names.")
                continue
            return ModelProbeResponse(
                reachable=True,
                models=models,
                latency_ms=round((time.perf_counter() - start_t) * 1000.0, 1),
            )
    error = " ".join(dict.fromkeys(errors))
    if _detect_provider(base_url) == "Ollama":
        error += " Ensure Ollama is running and reachable from the AutoSWE API container."
    return ModelProbeResponse(
        reachable=False,
        models=[],
        latency_ms=round((time.perf_counter() - start_t) * 1000.0, 1),
        error=error,
    )


@router.post("/models/test", response_model=ModelTestResponse)
async def test_model(request: ModelTestRequest, services: Services) -> ModelTestResponse:
    base_url = _normalize_backend_url(request.base_url)
    headers = await _model_check_headers(base_url, request.api_key, services)
    saved = await services.model_settings.load()
    timeout_seconds = request.timeout_seconds or saved.timeout_seconds
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
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            if resp.is_success:
                body = resp.json()
                choices = body.get("choices") if isinstance(body, dict) else None
                message = (
                    choices[0].get("message")
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                    else None
                )
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("missing response content")
                is_json = _parses_as_json(content)
                return ModelTestResponse(
                    success=True,
                    model=request.model,
                    latency_ms=round((time.perf_counter() - start_t) * 1000.0, 1),
                    structured_output=is_json,
                    response_snippet=content[:300],
                    error=None,
                )
            error = f"Model server returned HTTP {resp.status_code}."
            if resp.status_code in (401, 403):
                error += " Check the provider API key and access."
            elif resp.status_code == 404:
                error += " Check the model name and provider base URL."
    except httpx.InvalidURL:
        error = "Invalid provider URL. Enter a valid HTTP or HTTPS base URL."
    except httpx.TimeoutException:
        error = (
            f"Model response timed out after {timeout_seconds:g} seconds. "
            "Try a faster model or increase Inference Timeout."
        )
    except httpx.RequestError:
        error = (
            "Could not connect to the model server. "
            "Check the endpoint and that the server is running."
        )
    except (ValueError, TypeError):
        error = (
            "The model server returned an invalid completion response. "
            "Check the endpoint supports chat completions."
        )
    return ModelTestResponse(
        success=False,
        model=request.model,
        latency_ms=round((time.perf_counter() - start_t) * 1000.0, 1),
        structured_output=False,
        response_snippet="",
        error=error,
    )


@router.post("/projects/onboard", response_model=ProjectOnboardResponse, status_code=201)
async def onboard_project(
    request: ProjectOnboardRequest,
    services: Services,
) -> ProjectOnboardResponse:
    import_root = services.settings.repository_import_root.resolve(strict=False)
    branch_check = _run_git(
        import_root if import_root.is_dir() else Path.cwd(),
        "check-ref-format",
        "--branch",
        request.default_branch,
    )
    if branch_check.returncode or branch_check.stdout.strip() != request.default_branch:
        raise HTTPException(status_code=422, detail="Enter a valid Git branch name.")

    uploaded: list[tuple[Path, str]] = []
    paths: set[tuple[str, ...]] = set()
    for file_info in request.files:
        parts = file_info.path.split("/")
        if (
            "\\" in file_info.path
            or "\x00" in file_info.path
            or any(
                part in {"", ".", ".."} or ":" in part or part.casefold() == ".git"
                for part in parts
            )
        ):
            raise HTTPException(
                status_code=422, detail="Uploaded files must use safe repository-relative paths."
            )
        folded = tuple(part.casefold() for part in parts)
        if folded in paths:
            raise HTTPException(status_code=422, detail="Uploaded file paths must be unique.")
        paths.add(folded)
        uploaded.append((Path(*parts), file_info.content))
    if any(path[:depth] in paths for path in paths for depth in range(1, len(path))):
        raise HTTPException(status_code=422, detail="Uploaded file and directory paths conflict.")

    if uploaded:
        requested = (request.folder_name or request.source_path or request.name).strip()
        import_root.mkdir(parents=True, exist_ok=True)
        target_dir = _validated_repository_source(import_root, requested, must_exist=False)
        if target_dir == import_root:
            raise HTTPException(
                status_code=422, detail="Choose a new folder inside the import root."
            )
    else:
        requested = (request.source_path or request.folder_name).strip()
        target_dir = _validated_repository_source(import_root, requested)

    created_import = False
    try:
        if uploaded:
            try:
                # Atomic ownership claim: never reuse a folder belonging to a
                # prior upload or an existing repository, even with no Git data.
                target_dir.mkdir()
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Import folder already exists. Choose a new folder name.",
                ) from exc
            created_import = True
            for relative_path, content in uploaded:
                destination = target_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8") as stream:
                    stream.write(content)

        baseline_sha = _ensure_git_repository(
            target_dir, request.default_branch, initialize=bool(uploaded)
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
                source_path=str(target_dir),
                default_branch=request.default_branch,
            )
    except BaseException as exc:
        # This request may remove only the new directory it successfully claimed.
        # A failed connection/transaction must never alter an existing repository.
        if created_import:
            shutil.rmtree(target_dir)
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=409, detail="Project or repository ID already exists."
            ) from exc
        if isinstance(exc, OSError):
            raise HTTPException(
                status_code=422,
                detail="Could not create the uploaded repository. Check its folder and file paths.",
            ) from exc
        raise

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
            run = await services.database_repository.create_run(
                session,
                run_id=request.run_id,
                project_id=request.project_id,
                repository_id=request.repository_id,
                goal=request.goal,
                baseline_commit=request.baseline_commit,
            )
            configuration = await services.model_settings.load(session)
            run.model_configuration = configuration.private_storage()
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


async def _build_run_response(
    session: AsyncSession, run: RunRow
) -> RunResponse:
    run_id = run.id
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
    project_name = await session.scalar(
        select(ProjectRow.name).where(ProjectRow.id == run.project_id)
    )
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
        project_name=project_name,
    )


@router.get("/runs", response_model=tuple[RunResponse, ...])
async def list_runs(
    services: Services,
    project_id: UUID | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> tuple[RunResponse, ...]:
    if status is not None and status not in RUN_TERMINAL_VALUES and status not in {
        RunStatus.PENDING.value,
        RunStatus.PLANNING.value,
        RunStatus.EXECUTING.value,
        RunStatus.WAITING_FOR_APPROVAL.value,
        RunStatus.WAITING_FOR_MEMORY.value,
    }:
        raise HTTPException(status_code=422, detail=f"unknown run status {status!r}")
    async with services.database.sessions() as session:
        statement = (
            select(RunRow)
            .order_by(RunRow.created_at.desc(), RunRow.id)
            .offset(offset)
            .limit(limit)
        )
        if project_id is not None:
            statement = statement.where(RunRow.project_id == project_id)
        if status is not None:
            statement = statement.where(RunRow.state == status)
        runs = tuple((await session.scalars(statement)).all())
        responses: list[RunResponse] = []
        for run in runs:
            responses.append(await _build_run_response(session, run))
    return tuple(responses)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, services: Services) -> RunResponse:
    async with services.database.sessions() as session:
        run = await session.get(RunRow, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return await _build_run_response(session, run)


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


@router.get("/runs/{run_id}/history", response_model=HistoryResponse)
async def get_run_history(
    run_id: UUID,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> HistoryResponse:
    async with services.database.sessions() as session:
        await _require_run(session, run_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ModelCallRow)
                    .where(ModelCallRow.run_id == run_id)
                    .order_by(ModelCallRow.created_at.asc(), ModelCallRow.id.asc())
                    .limit(limit)
                )
            ).all()
        )
    samples = tuple(
        HistorySample(
            timestamp=row.created_at.isoformat(),
            input_tokens=int(row.input_tokens or 0),
            output_tokens=int(row.output_tokens or 0),
            cost_usd=float(row.cost_usd or 0.0),
            model=str(row.model or ""),
        )
        for row in rows
    )
    return HistoryResponse(run_id=run_id, samples=samples)


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


@router.get(
    "/projects/{project_id}/tasks/{task_id}/messages",
    response_model=tuple[MessageResponse, ...],
)
async def list_task_messages(
    project_id: UUID,
    task_id: UUID,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> tuple[MessageResponse, ...]:
    async with services.database.sessions() as session:
        row = await services.database_repository.get_task(
            session,
            project_id=project_id,
            task_id=task_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        messages = tuple(
            (
                await session.scalars(
                    select(AgentMessageRow)
                    .where(AgentMessageRow.task_id == task_id)
                    .order_by(AgentMessageRow.created_at.desc(), AgentMessageRow.id.desc())
                    .limit(limit)
                )
            ).all()
        )
    result: list[MessageResponse] = []
    for message in messages:
        payload = message.payload if isinstance(message.payload, dict) else {}
        summary = str(payload.get("summary", ""))[:20_000]
        result.append(
            MessageResponse(
                message_id=message.id,
                task_id=message.task_id,
                kind=message.kind,
                sender=message.sender,
                recipient=message.recipient,
                summary=summary or "(no summary recorded)",
                created_at=message.created_at.isoformat(),
            )
        )
    return tuple(result)


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


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: UUID, services: Services) -> dict[str, str]:
    async with services.database.transaction() as session:
        run = await session.scalar(
            select(RunRow).where(RunRow.id == run_id).with_for_update()
        )
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.state in RUN_TERMINAL_VALUES:
            raise HTTPException(
                status_code=409, detail=f"run already {run.state}"
            )
        now = utc_now()
        if run.cancellation_requested_at is None:
            run.cancellation_requested_at = now
        # PENDING/PLANNING runs have no dispatched tasks yet; the dispatcher
        # loop may be blocked inside the planner's long model call, so the
        # asynchronous sweep would never run. Transition synchronously.
        if run.state in (RunStatus.PENDING.value, RunStatus.PLANNING.value):
            from domain.events import require_run_transition

            require_run_transition(RunStatus(run.state), RunStatus.CANCELLED)
            await services.database_repository.record_state_duration(
                session,
                aggregate_type="workflow",
                aggregate_id=run.id,
                state=run.state,
                entered_at=run.state_entered_at,
                exited_at=now,
            )
            run.state = RunStatus.CANCELLED.value
            run.state_entered_at = now
            attempt = await session.scalar(
                select(RunStageAttemptRow)
                .where(RunStageAttemptRow.run_id == run.id)
                .order_by(RunStageAttemptRow.started_at.desc())
                .limit(1)
            )
            if attempt is not None and attempt.status not in ("CANCELLED", "FAILED", "COMPLETED"):
                attempt.status = "CANCELLED"
                attempt.ended_at = now
            event_id = uuid4()
            payload = {"run_id": str(run.id), "reason": "operator_cancelled"}
            await services.database_repository.append_audit(
                session,
                event_id=event_id,
                event_type="run.cancelled",
                aggregate_type="run",
                aggregate_id=run.id,
                payload=payload,
                correlation_id=run.id,
                causation_id=event_id,
            )
            await services.database_repository.enqueue_event(
                session,
                event_id=event_id,
                topic="run-state",
                payload=payload,
            )
            return {"run_id": str(run_id), "status": "CANCELLED"}
    return {"run_id": str(run_id), "status": "CANCELLATION_REQUESTED"}


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
    except Exception as error:
        logger.warning(
            "websocket_event_stream_rejected",
            error_type=type(error).__name__,
            project_id=str(project_id),
            task_id=str(task_id),
        )
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
        description=row.description,
        priority=row.priority,
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


def _ensure_git_repository(
    target_dir: Path, default_branch: str = "main", *, initialize: bool = False
) -> str:
    if initialize:
        commands = (
            ("init", "--template=", "-b", default_branch),
            ("add", "--all", "--force", "--", "."),
            ("commit", "-m", "Initial imported baseline", "--allow-empty"),
        )
        for command in commands:
            result = _run_git(
                target_dir,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=AutoSWE System",
                "-c",
                "user.email=autoswe@local",
                *command,
            )
            if result.returncode:
                raise HTTPException(
                    status_code=422, detail="Could not initialize the uploaded repository baseline."
                )
    else:
        # Git can discover a parent repository from an arbitrary child folder.
        # Require the selected folder itself to be the repository root.
        bare = _run_git(target_dir, "rev-parse", "--is-bare-repository")
        location = _run_git(
            target_dir,
            "rev-parse",
            "--absolute-git-dir" if bare.stdout.strip() == "true" else "--show-toplevel",
        )
        if location.returncode or Path(location.stdout.strip()).resolve() != target_dir:
            raise HTTPException(status_code=422, detail="Select an existing Git repository root.")

    result = _run_git(
        target_dir, "rev-parse", "--verify", f"refs/heads/{default_branch}^{{commit}}"
    )
    baseline = result.stdout.strip()
    if result.returncode or re.fullmatch(r"[0-9a-f]{40}", baseline) is None:
        raise HTTPException(
            status_code=422,
            detail="The requested branch has no committed baseline in this repository.",
        )
    return baseline


def _validated_repository_source(
    import_root: Path, requested: str, *, must_exist: bool = True
) -> Path:
    try:
        if not requested.strip() or "\x00" in requested:
            raise ValueError("repository path is required")
        root = import_root.resolve(strict=True)
        req_path = Path(requested)
        candidate = (root / req_path if not req_path.is_absolute() else req_path).resolve(
            strict=must_exist
        )
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="repository source must be an existing path inside the import root",
        ) from exc
    if candidate.is_symlink() or (must_exist and not candidate.is_dir()):
        raise HTTPException(status_code=422, detail="repository source must be a directory")
    return candidate


def _safe_delivery_error(value: str) -> str:
    lowered = value.casefold()
    sensitive_markers = ("bearer ", "api_key", "token=", "password=", "secret=")
    if any(marker in lowered for marker in sensitive_markers):
        return "delivery failed; sensitive details redacted"
    return value[:1_000]
