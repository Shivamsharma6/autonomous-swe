from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import ArtifactState
from domain.models import ArtifactRef
from persistence.repositories import DomainRepository


class ArtifactError(RuntimeError):
    pass


class ArtifactPathError(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: UUID
    sha256: str
    media_type: str
    size_bytes: int
    storage_key: str


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    valid: bool
    expected_sha256: str
    actual_sha256: str | None
    expected_size_bytes: int
    actual_size_bytes: int | None


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.root = root.resolve(strict=True)

    def put(
        self,
        content: bytes,
        *,
        artifact_id: UUID,
        media_type: str,
    ) -> StoredArtifact:
        digest = hashlib.sha256(content).hexdigest()
        storage_key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        descriptor = StoredArtifact(
            artifact_id=artifact_id,
            sha256=digest,
            media_type=media_type,
            size_bytes=len(content),
            storage_key=storage_key,
        )
        destination = self._safe_path(storage_key, require_exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._require_contained(destination.parent.resolve(strict=True))

        if destination.exists():
            if not self.verify(descriptor).valid:
                raise ArtifactIntegrityError(
                    "existing content-addressed object does not match its SHA-256"
                )
            return descriptor

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o440)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if not self.verify(descriptor).valid:
                    raise ArtifactIntegrityError(
                        "concurrent content-addressed object does not match its SHA-256"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
        return descriptor

    def verify(self, artifact: StoredArtifact) -> ArtifactVerification:
        try:
            stream = self._open_contained(artifact.storage_key)
        except (ArtifactPathError, FileNotFoundError):
            return ArtifactVerification(
                valid=False,
                expected_sha256=artifact.sha256,
                actual_sha256=None,
                expected_size_bytes=artifact.size_bytes,
                actual_size_bytes=None,
            )
        with stream:
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1_048_576):
                digest.update(chunk)
                size += len(chunk)
        actual = digest.hexdigest()
        return ArtifactVerification(
            valid=actual == artifact.sha256 and size == artifact.size_bytes,
            expected_sha256=artifact.sha256,
            actual_sha256=actual,
            expected_size_bytes=artifact.size_bytes,
            actual_size_bytes=size,
        )

    def open_verified(self, artifact: StoredArtifact) -> BinaryIO:
        stream = self._open_contained(artifact.storage_key)
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1_048_576):
            digest.update(chunk)
            size += len(chunk)
        actual = digest.hexdigest()
        if actual != artifact.sha256 or size != artifact.size_bytes:
            stream.close()
            raise ArtifactIntegrityError(
                f"artifact SHA-256 or size mismatch: expected {artifact.sha256}, got {actual}"
            )
        stream.seek(0)
        return stream

    def read_verified(self, artifact: StoredArtifact) -> bytes:
        with self.open_verified(artifact) as stream:
            return stream.read()

    def quarantine(self, artifact: StoredArtifact) -> Path:
        source = self._safe_path(artifact.storage_key, require_exists=True)
        quarantine_root = self.root / "quarantine" / str(artifact.artifact_id)
        quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._require_contained(quarantine_root.resolve(strict=True))
        target = quarantine_root / artifact.sha256
        os.replace(source, target)
        target.chmod(0o400)
        return target

    def _open_contained(self, storage_key: str) -> BinaryIO:
        path = self._safe_path(storage_key, require_exists=True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ArtifactPathError("artifact object must be a regular file")
        return os.fdopen(descriptor, "rb")

    def _safe_path(self, storage_key: str, *, require_exists: bool) -> Path:
        key = PurePosixPath(storage_key)
        if key.is_absolute() or not key.parts or ".." in key.parts or ":" in key.parts[0]:
            raise ArtifactPathError("artifact storage key escapes the artifact root")
        candidate = self.root.joinpath(*key.parts)
        if require_exists:
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError:
                raise
            self._require_contained(resolved)
            if candidate.is_symlink():
                raise ArtifactPathError("artifact object cannot be a symlink")
        else:
            self._require_contained(candidate.parent.resolve(strict=False))
        return candidate

    def _require_contained(self, path: Path) -> None:
        if path != self.root and self.root not in path.parents:
            raise ArtifactPathError("artifact path escapes the artifact root")


class ArtifactService:
    def __init__(self, *, store: ArtifactStore, repository: DomainRepository) -> None:
        self.store = store
        self.repository = repository

    async def put(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        media_type: str,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID,
    ) -> StoredArtifact:
        stored = self.store.put(content, artifact_id=uuid4(), media_type=media_type)
        await self.repository.record_artifact(
            session,
            artifact=ArtifactRef(
                artifact_id=stored.artifact_id,
                sha256=stored.sha256,
                media_type=stored.media_type,
            ),
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
        )
        return stored

    async def get_verified(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        artifact_id: UUID,
    ) -> bytes:
        row = await self.repository.get_artifact(
            session, project_id=project_id, artifact_id=artifact_id, for_update=True
        )
        if row is None:
            raise LookupError(f"artifact {artifact_id} does not exist in project {project_id}")
        if row.state is not ArtifactState.VALID:
            raise ArtifactIntegrityError(f"artifact {artifact_id} is not valid evidence")
        stored = StoredArtifact(
            artifact_id=row.id,
            sha256=row.sha256,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            storage_key=row.storage_key,
        )
        try:
            return self.store.read_verified(stored)
        except (ArtifactIntegrityError, ArtifactPathError, FileNotFoundError):
            await self.repository.mark_artifact_corrupt(
                session,
                project_id=project_id,
                artifact_id=artifact_id,
            )
            try:
                self.store.quarantine(stored)
            except (ArtifactError, FileNotFoundError):
                pass
            raise
