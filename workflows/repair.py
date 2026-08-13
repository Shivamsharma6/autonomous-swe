from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    ContractModel,
    Sha256,
    TaskPlan,
    TaskPlanMutation,
    canonical_sha256,
)
from persistence.artifacts import ArtifactError, ArtifactService
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import ArtifactRow, PlanRevisionRow, RepairMutationRow, RunRow
from planning.validator import TaskPlanValidator


class RepairAction(StrEnum):
    COMPLETE = "COMPLETE"
    APPLY_MUTATION = "APPLY_MUTATION"
    TERMINATE = "TERMINATE"


class VerificationOutcome(ContractModel):
    schema_version: str = "1.0"
    passed: bool
    failure_signature: Sha256 | None = None
    progress_fingerprint: Sha256
    artifact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def signature_matches_outcome(self) -> VerificationOutcome:
        if self.passed and self.failure_signature is not None:
            raise ValueError("successful verification cannot have a failure signature")
        if not self.passed and self.failure_signature is None:
            raise ValueError("failed verification requires a failure signature")
        return self


class RepairHistory(ContractModel):
    failure_signature: Sha256
    progress_fingerprint: Sha256
    accepted_revision: int = Field(ge=2)


@dataclass(frozen=True, slots=True)
class RepairDecision:
    action: RepairAction
    reason_codes: tuple[str, ...]
    plan: TaskPlan | None
    mutation_id: UUID | None = None
    replayed: bool = False


class RepairPolicy:
    def __init__(self, validator: TaskPlanValidator) -> None:
        self._validator = validator

    def decide(
        self,
        current: TaskPlan,
        outcome: VerificationOutcome,
        *,
        mutation: TaskPlanMutation | None,
        history: tuple[RepairHistory, ...],
    ) -> RepairDecision:
        if outcome.passed:
            return RepairDecision(RepairAction.COMPLETE, (), current)
        assert outcome.failure_signature is not None
        if any(item.failure_signature == outcome.failure_signature for item in history):
            return RepairDecision(
                RepairAction.TERMINATE,
                ("REPEATED_FAILURE_SIGNATURE",),
                None,
                mutation.mutation_id if mutation else None,
            )
        if any(item.progress_fingerprint == outcome.progress_fingerprint for item in history):
            return RepairDecision(
                RepairAction.TERMINATE,
                ("NO_PROGRESS",),
                None,
                mutation.mutation_id if mutation else None,
            )
        if mutation is None:
            return RepairDecision(
                RepairAction.TERMINATE,
                ("MISSING_REPAIR_MUTATION",),
                None,
            )
        validation = self._validator.validate_mutation(current, mutation)
        if not validation.valid or validation.proposed_plan is None:
            return RepairDecision(
                RepairAction.TERMINATE,
                tuple(dict.fromkeys(issue.code for issue in validation.issues)),
                None,
                mutation.mutation_id,
            )
        return RepairDecision(
            RepairAction.APPLY_MUTATION,
            (),
            validation.proposed_plan,
            mutation.mutation_id,
        )


class RepairIntegrityError(RuntimeError):
    pass


class DurableRepairController:
    def __init__(
        self,
        database: Database,
        *,
        validator: TaskPlanValidator,
        artifacts: ArtifactService,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._policy = RepairPolicy(validator)
        self._artifacts = artifacts
        self._repository = repository or DomainRepository()

    async def apply(
        self,
        *,
        run_id: UUID,
        outcome: VerificationOutcome,
        mutation: TaskPlanMutation,
    ) -> RepairDecision:
        if outcome.passed:
            raise ValueError("a successful verification does not require a repair mutation")
        mutation_hash = canonical_sha256(mutation)
        async with self._database.transaction() as session:
            run = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            existing = await session.get(RepairMutationRow, mutation.mutation_id)
            if existing is not None:
                return await _replayed_decision(
                    session,
                    existing,
                    run_id=run_id,
                    mutation_hash=mutation_hash,
                    outcome=outcome,
                )
            current_row = await session.scalar(
                select(PlanRevisionRow)
                .where(PlanRevisionRow.run_id == run_id)
                .order_by(PlanRevisionRow.revision.desc())
                .limit(1)
                .with_for_update()
            )
            if current_row is None:
                raise LookupError(f"run {run_id} has no durable plan revision")
            current = TaskPlan.model_validate(current_row.plan)
            history_rows = tuple(
                (
                    await session.scalars(
                        select(RepairMutationRow)
                        .where(
                            RepairMutationRow.run_id == run_id,
                            RepairMutationRow.status == RepairAction.APPLY_MUTATION.value,
                        )
                        .order_by(RepairMutationRow.accepted_revision)
                    )
                ).all()
            )
            history: list[RepairHistory] = []
            for history_row in history_rows:
                if history_row.accepted_revision is None:
                    raise RepairIntegrityError(
                        "accepted repair history is missing its plan revision"
                    )
                history.append(
                    RepairHistory(
                        failure_signature=history_row.failure_signature,
                        progress_fingerprint=history_row.progress_fingerprint,
                        accepted_revision=history_row.accepted_revision,
                    )
                )
            evidence_errors = await self._verify_evidence(
                session,
                project_id=run.project_id,
                run_id=run_id,
                artifact_ids=outcome.artifact_ids,
            )
            decision = (
                RepairDecision(
                    RepairAction.TERMINATE,
                    evidence_errors,
                    None,
                    mutation.mutation_id,
                )
                if evidence_errors
                else self._policy.decide(
                    current,
                    outcome,
                    mutation=mutation,
                    history=tuple(history),
                )
            )
            accepted_revision = (
                decision.plan.revision
                if decision.action is RepairAction.APPLY_MUTATION and decision.plan
                else None
            )
            row = RepairMutationRow(
                mutation_id=mutation.mutation_id,
                run_id=run_id,
                base_revision=mutation.base_revision,
                accepted_revision=accepted_revision,
                failure_signature=outcome.failure_signature or "0" * 64,
                progress_fingerprint=outcome.progress_fingerprint,
                mutation_hash=mutation_hash,
                verification_artifact_ids=[str(value) for value in outcome.artifact_ids],
                status=decision.action.value,
                reason_codes=list(decision.reason_codes),
                mutation=mutation.model_dump(mode="json"),
            )
            session.add(row)
            if decision.action is RepairAction.APPLY_MUTATION:
                assert decision.plan is not None
                await self._repository.create_plan_revision(
                    session,
                    run_id=run_id,
                    revision=decision.plan.revision,
                    plan=decision.plan.model_dump(mode="json"),
                )
                for task in mutation.tasks:
                    await self._repository.create_task(session, run_id=run_id, task=task)
            event_id = uuid5(NAMESPACE_URL, f"repair-decision:{mutation.mutation_id}")
            event_type = (
                "plan.repair_accepted"
                if decision.action is RepairAction.APPLY_MUTATION
                else "plan.repair_terminated"
            )
            payload = {
                "mutation_id": str(mutation.mutation_id),
                "run_id": str(run_id),
                "action": decision.action.value,
                "base_revision": mutation.base_revision,
                "accepted_revision": accepted_revision,
                "reason_codes": list(decision.reason_codes),
            }
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type=event_type,
                aggregate_type="run",
                aggregate_id=run_id,
                payload=payload,
                correlation_id=run_id,
                causation_id=mutation.mutation_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="plan-repair",
                payload=payload,
            )
            await session.flush()
            return decision

    async def _verify_evidence(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        run_id: UUID,
        artifact_ids: tuple[UUID, ...],
    ) -> tuple[str, ...]:
        errors: list[str] = []
        for artifact_id in artifact_ids:
            try:
                await self._artifacts.get_verified(
                    session,
                    project_id=project_id,
                    artifact_id=artifact_id,
                )
            except (ArtifactError, FileNotFoundError, LookupError):
                errors.append("INVALID_REPAIR_EVIDENCE")
                continue
            row = await session.get(ArtifactRow, artifact_id)
            if row is None or row.run_id != run_id:
                errors.append("REPAIR_EVIDENCE_RUN_MISMATCH")
        return tuple(dict.fromkeys(errors))


async def _replayed_decision(
    session: AsyncSession,
    row: RepairMutationRow,
    *,
    run_id: UUID,
    mutation_hash: str,
    outcome: VerificationOutcome,
) -> RepairDecision:
    if (
        row.run_id != run_id
        or row.mutation_hash != mutation_hash
        or row.failure_signature != outcome.failure_signature
        or row.progress_fingerprint != outcome.progress_fingerprint
        or row.verification_artifact_ids != [str(value) for value in outcome.artifact_ids]
    ):
        raise RepairIntegrityError("mutation_id replay does not match its original run and content")
    plan: TaskPlan | None = None
    if row.accepted_revision is not None:
        plan_row = await session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.run_id == run_id,
                PlanRevisionRow.revision == row.accepted_revision,
            )
        )
        if plan_row is None:
            raise RepairIntegrityError("accepted repair revision is missing")
        plan = TaskPlan.model_validate(plan_row.plan)
    return RepairDecision(
        action=RepairAction(row.status),
        reason_codes=tuple(row.reason_codes),
        plan=plan,
        mutation_id=row.mutation_id,
        replayed=True,
    )
