from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain.enums import (
    ApprovalStatus,
    ArtifactState,
    GraphExecutionState,
    TaskStatus,
    TaskType,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(enum_type: type[Any]) -> list[str]:
    return [member.value for member in enum_type]


TASK_STATUS_ENUM = Enum(
    TaskStatus,
    name="task_status",
    values_callable=enum_values,
    validate_strings=True,
)
TASK_TYPE_ENUM = Enum(
    TaskType,
    name="task_type",
    values_callable=enum_values,
    validate_strings=True,
)
GRAPH_STATE_ENUM = Enum(
    GraphExecutionState,
    name="graph_execution_state",
    values_callable=enum_values,
    validate_strings=True,
)
APPROVAL_STATUS_ENUM = Enum(
    ApprovalStatus,
    name="approval_status",
    values_callable=enum_values,
    validate_strings=True,
)
ARTIFACT_STATE_ENUM = Enum(
    ArtifactState,
    name="artifact_state",
    values_callable=enum_values,
    validate_strings=True,
)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProjectRow(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)


class RepositoryRow(TimestampMixin, Base):
    __tablename__ = "repositories"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (Index("ix_repositories_project", "project_id"),)


class RunRow(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("length(baseline_commit) >= 40", name="ck_runs_baseline_commit"),
        Index("ix_runs_project_state", "project_id", "state"),
    )


class PlanRevisionRow(Base):
    __tablename__ = "plan_revisions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_plan_revision"),
        CheckConstraint("revision >= 1", name="ck_plan_revision_positive"),
    )


class TaskRow(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[TaskType] = mapped_column(TASK_TYPE_ENUM, nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assigned_capability: Mapped[str] = mapped_column(String(100), nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    risk_ceiling: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_artifacts: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    repository_paths: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    estimate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[TaskStatus] = mapped_column(
        TASK_STATUS_ENUM, default=TaskStatus.PENDING, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_tasks_version_positive"),
        Index("ix_tasks_ready", "state", "priority", "created_at"),
        Index("ix_tasks_project_state", "project_id", "state"),
    )


class TaskAttemptRow(Base):
    __tablename__ = "task_attempts"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="RUNNING", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("length(agent_spec_hash) = 64", name="ck_attempt_agent_spec_hash"),
        Index("ix_attempts_task", "task_id", "started_at"),
    )


class LeaseRow(Base):
    __tablename__ = "leases"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    token: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (Index("ix_leases_expiry", "expires_at"),)


class ReservationRow(Base):
    __tablename__ = "reservations"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    resource: Mapped[str] = mapped_column(String(50), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("units > 0", name="ck_reservation_units"),
        Index("ix_reservations_active", "resource", "project_id", "released_at"),
    )


class ProjectTaskResourceEstimateRow(Base):
    __tablename__ = "project_task_resource_estimates"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[TaskType] = mapped_column(TASK_TYPE_ENUM, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_cpu_time_ms: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    peak_memory_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    average_duration_ms: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    average_output_bytes: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    average_network_requests: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    average_model_tokens: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    average_cost_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("project_id", "task_type", name="uq_project_task_resource_estimate"),
        CheckConstraint("sample_count >= 0", name="ck_resource_estimate_sample_count"),
        CheckConstraint(
            "average_cpu_time_ms >= 0 AND peak_memory_bytes >= 0 "
            "AND average_duration_ms >= 0 AND average_output_bytes >= 0 "
            "AND average_network_requests >= 0 AND average_model_tokens >= 0 "
            "AND average_cost_usd >= 0",
            name="ck_resource_estimates_nonnegative",
        ),
    )


class GraphExecutionRow(TimestampMixin, Base):
    __tablename__ = "graph_executions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    state: Mapped[GraphExecutionState] = mapped_column(GRAPH_STATE_ENUM, nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(500))
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        CheckConstraint("length(baseline_commit) >= 40", name="ck_graph_execution_baseline_commit"),
        Index("ix_graph_execution_state", "state", "state_entered_at"),
    )


class AgentMessageRow(Base):
    __tablename__ = "agent_messages"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    sender: Mapped[str] = mapped_column(String(100), nullable=False)
    recipient: Mapped[str] = mapped_column(String(100), nullable=False)
    causation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    artifact_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retained_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("length(content_hash) = 64", name="ck_message_content_hash"),
        Index("ix_messages_task_created", "task_id", "created_at"),
    )


class OutboxRow(Base):
    __tablename__ = "outbox"

    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        Index("ix_outbox_publishable", "published_at", "next_attempt_at"),
    )


class ConsumerReceiptRow(Base):
    __tablename__ = "consumer_receipts"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    consumer: Mapped[str] = mapped_column(String(255), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (UniqueConstraint("consumer", "event_id", name="uq_consumer_event_receipt"),)


class DeadLetterRow(Base):
    __tablename__ = "dead_letters"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    causation_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("attempts > 0", name="ck_dead_letter_attempts"),
        Index("ix_dead_letters_unresolved", "resolved_at", "created_at"),
    )


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    call_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tool_executions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    baseline_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    call_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        APPROVAL_STATUS_ENUM, default=ApprovalStatus.PENDING, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approver: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (Index("ix_approvals_pending", "status", "expires_at"),)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[ArtifactState] = mapped_column(ARTIFACT_STATE_ENUM, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="ck_artifact_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size"),
        Index("ix_artifacts_hash", "sha256"),
        Index("ix_artifacts_evidence", "task_id", "state"),
    )


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    classification: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    candidate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    deterministic_memory_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_memory_candidates_status", "status", "created_at"),)


class SandboxExecutionRow(Base):
    __tablename__ = "sandbox_executions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    cpu_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peak_memory_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peak_processes: Mapped[int] = mapped_column(Integer, nullable=False)
    processes_created: Mapped[int | None] = mapped_column(Integer)
    stdout_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stderr_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    network_requests: Mapped[int] = mapped_column(BigInteger, nullable=False)
    network_bytes_sent: Mapped[int] = mapped_column(BigInteger, nullable=False)
    network_bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    exit_reason: Mapped[str] = mapped_column(String(100), nullable=False)
    limit_triggered: Mapped[str | None] = mapped_column(String(100))
    measurement_source: Mapped[str] = mapped_column(String(100), nullable=False)
    measurement_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "cpu_time_ms >= 0 AND peak_memory_bytes >= 0 AND duration_ms >= 0",
            name="ck_sandbox_usage_nonnegative",
        ),
        Index("ix_sandbox_task_created", "task_id", "created_at"),
    )


class StateDurationRow(Base):
    __tablename__ = "state_durations"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (
        CheckConstraint("duration_seconds >= 0", name="ck_state_duration_nonnegative"),
        Index("ix_state_duration_aggregate", "aggregate_type", "aggregate_id"),
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    __table_args__ = (
        CheckConstraint("length(content_hash) = 64", name="ck_audit_content_hash"),
        Index("ix_audit_aggregate", "aggregate_type", "aggregate_id", "created_at"),
    )
