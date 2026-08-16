import contextlib
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket
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
    ProjectCreated,
    ProjectCreateRequest,
    RunCreated,
    RunCreateRequest,
    RunResponse,
    TaskResponse,
)
from apps.api.websocket import EventCursor, PostgresTaskEventSource
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


@router.get("/status")
async def status() -> dict[str, str]:
    return {"service": "autoswe-control-plane", "status": "ok"}


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


def _validated_repository_source(import_root: Path, requested: str) -> Path:
    try:
        root = import_root.resolve(strict=True)
        candidate = Path(requested).resolve(strict=True)
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
