from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apps.api.dependencies import (
    ControlPlaneServices,
    get_services,
    require_admin,
    require_websocket_admin,
)
from apps.api.schemas import (
    ApprovalDecisionRequest,
    DeadLetterResponse,
    ProjectCreated,
    ProjectCreateRequest,
    RunCreated,
    RunCreateRequest,
    TaskResponse,
)
from apps.api.websocket import EventCursor, PostgresTaskEventSource
from persistence.artifacts import ArtifactIntegrityError, ArtifactPathError
from persistence.tables import DeadLetterRow, TaskRow, utc_now
from tools.approval import ApprovalBindingError, ApprovalExpired

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_admin)],
)
Services = Annotated[ControlPlaneServices, Depends(get_services)]
DeadLetterLimit = Annotated[int, Query(ge=1, le=500)]


@router.get("/status")
async def status() -> dict[str, str]:
    return {"service": "autoswe-control-plane", "status": "ok"}


@router.post("/projects", response_model=ProjectCreated, status_code=201)
async def create_project(
    request: ProjectCreateRequest,
    services: Services,
) -> ProjectCreated:
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
            source_path=request.source_path,
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
    )


def _safe_delivery_error(value: str) -> str:
    lowered = value.casefold()
    sensitive_markers = ("bearer ", "api_key", "token=", "password=", "secret=")
    if any(marker in lowered for marker in sensitive_markers):
        return "delivery failed; sensitive details redacted"
    return value[:1_000]
