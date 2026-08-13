from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from persistence.artifacts import (
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactStore,
    StoredArtifact,
)


def test_put_is_content_addressed_atomic_and_deduplicated(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    content = b"pytest: 42 passed\n"

    first = store.put(
        content,
        artifact_id=uuid4(),
        media_type="text/plain",
    )
    second = store.put(
        content,
        artifact_id=uuid4(),
        media_type="text/plain",
    )

    assert first.sha256 == second.sha256
    assert first.storage_key == second.storage_key
    assert first.storage_key == f"sha256/{first.sha256[:2]}/{first.sha256[2:4]}/{first.sha256}"
    assert first.size_bytes == len(content)
    assert list(store.root.rglob("*.tmp")) == []
    assert store.read_verified(first) == content


def test_verify_never_trusts_descriptor_digest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    stored = store.put(b"verified bytes", artifact_id=uuid4(), media_type="text/plain")
    dishonest = StoredArtifact(
        artifact_id=stored.artifact_id,
        sha256="0" * 64,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        storage_key=stored.storage_key,
    )

    assert store.verify(stored).valid is True
    assert store.verify(dishonest).valid is False
    with pytest.raises(ArtifactIntegrityError):
        store.read_verified(dishonest)


@pytest.mark.parametrize("storage_key", ["../secret", "/etc/passwd", "sha256/../../secret"])
def test_open_rejects_path_escape(tmp_path: Path, storage_key: str) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    descriptor = StoredArtifact(
        artifact_id=uuid4(),
        sha256="a" * 64,
        media_type="text/plain",
        size_bytes=1,
        storage_key=storage_key,
    )

    with pytest.raises(ArtifactPathError):
        store.open_verified(descriptor)


def test_open_rejects_symlink_to_external_object(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    external = tmp_path / "external.txt"
    external.write_bytes(b"outside")
    link = store.root / "sha256" / "link"
    link.parent.mkdir(parents=True)
    os.symlink(external, link)
    descriptor = StoredArtifact(
        artifact_id=uuid4(),
        sha256="a" * 64,
        media_type="text/plain",
        size_bytes=7,
        storage_key="sha256/link",
    )

    with pytest.raises(ArtifactPathError):
        store.open_verified(descriptor)


def test_quarantine_moves_corrupt_object_out_of_evidence_tree(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    stored = store.put(b"original", artifact_id=uuid4(), media_type="text/plain")
    object_path = store.root / stored.storage_key
    object_path.chmod(0o600)
    object_path.write_bytes(b"corrupt")

    quarantined = store.quarantine(stored)

    assert quarantined.is_file()
    assert store.root in quarantined.parents
    assert "quarantine" in quarantined.parts
    assert not object_path.exists()
