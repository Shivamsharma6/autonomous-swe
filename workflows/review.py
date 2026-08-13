from __future__ import annotations

from uuid import UUID

from pydantic import Field

from domain.enums import ArtifactState
from domain.models import ContractModel, NonEmptyText, ReleaseDecision
from persistence.artifacts import ArtifactError, ArtifactService
from persistence.database import Database
from persistence.tables import ArtifactRow


class ReleaseReviewRequest(ContractModel):
    schema_version: str = "1.0"
    project_id: UUID
    run_id: UUID
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=100)
    proposed_evidence: dict[str, tuple[UUID, ...]] = Field(default_factory=dict)
    summary: NonEmptyText


class ReleaseReviewer:
    def __init__(self, *, database: Database, artifacts: ArtifactService) -> None:
        self._database = database
        self._artifacts = artifacts

    async def review(self, request: ReleaseReviewRequest) -> ReleaseDecision:
        evidence: dict[str, tuple[UUID, ...]] = {}
        failures: list[str] = []
        expected = set(request.acceptance_criteria)
        extras = sorted(set(request.proposed_evidence).difference(expected))
        for criterion in extras:
            failures.append(f"Unexpected evidence mapping: {criterion}")

        async with self._database.transaction() as session:
            for criterion in request.acceptance_criteria:
                proposed = tuple(dict.fromkeys(request.proposed_evidence.get(criterion, ())))
                if not proposed:
                    failures.append(f"{criterion}: no evidence was provided")
                    continue
                verified: list[UUID] = []
                for artifact_id in proposed:
                    try:
                        await self._artifacts.get_verified(
                            session,
                            project_id=request.project_id,
                            artifact_id=artifact_id,
                        )
                    except (ArtifactError, FileNotFoundError, LookupError) as error:
                        failures.append(f"{criterion}: artifact {artifact_id} invalid: {error}")
                        continue
                    row = await session.get(ArtifactRow, artifact_id)
                    if (
                        row is None
                        or row.run_id != request.run_id
                        or row.state is not ArtifactState.VALID
                        or row.verified_at is None
                    ):
                        failures.append(
                            f"{criterion}: artifact {artifact_id} is outside the verified run"
                        )
                        continue
                    verified.append(artifact_id)
                if verified:
                    evidence[criterion] = tuple(verified)
                else:
                    failures.append(f"{criterion}: no verified evidence remains")

        approved = not failures and all(
            criterion in evidence for criterion in request.acceptance_criteria
        )
        return ReleaseDecision(
            approved=approved,
            summary=request.summary,
            acceptance_evidence=evidence,
            failure_reasons=tuple(failures),
        )
