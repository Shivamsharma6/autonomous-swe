from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from persistence.tables import ProjectRow, RepositoryRow
from tests.integration.api.test_control_plane import ADMIN_TOKEN, services


def git(directory: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [executable, *args], cwd=directory, check=True, text=True, capture_output=True
    ).stdout.strip()


def repository_at(directory: Path) -> str:
    directory.mkdir(parents=True)
    git(directory, "init", "-b", "main")
    git(directory, "config", "user.name", "Onboarding Test")
    git(directory, "config", "user.email", "onboarding@example.test")
    (directory / "README.md").write_text("Original repository\n")
    git(directory, "add", "README.md")
    git(directory, "commit", "-m", "Baseline")
    return git(directory, "rev-parse", "HEAD")


def client_for(configured):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured), raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )


async def assert_no_records(database):
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 0
        assert await session.scalar(select(func.count()).select_from(RepositoryRow)) == 0


@pytest.mark.parametrize("absolute", [False, True])
async def test_existing_nested_repository_is_connected_without_modification(
    database, tmp_path, absolute
):
    configured = services(database, tmp_path)
    target = configured.settings.repository_import_root / "nested" / "existing"
    baseline = repository_at(target)
    git_config = (target / ".git" / "config").read_bytes()
    requested = str(target) if absolute else "nested/existing"

    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={"name": "Existing", "source_path": requested, "default_branch": "main"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["source_path"] == str(target)
    assert response.json()["baseline_commit"] == baseline
    assert git(target, "status", "--porcelain") == ""
    assert (target / ".git" / "config").read_bytes() == git_config
    assert not (configured.settings.repository_import_root / "existing").exists()


async def test_existing_repository_uses_requested_branch_without_checkout(database, tmp_path):
    configured = services(database, tmp_path)
    target = configured.settings.repository_import_root / "branches"
    main_sha = repository_at(target)
    git(target, "checkout", "-b", "feature/subtract")
    (target / "README.md").write_text("Feature branch\n")
    git(target, "commit", "-am", "Feature")
    feature_sha = git(target, "rev-parse", "HEAD")
    git(target, "checkout", "main")

    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={
                "name": "Feature",
                "folder_name": "branches",
                "default_branch": "feature/subtract",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["baseline_commit"] == feature_sha != main_sha
    assert git(target, "branch", "--show-current") == "main"
    assert git(target, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "kind", ["outside", "symlink_outside", "not_git", "missing_branch", "subdirectory"]
)
async def test_existing_repository_rejects_invalid_source_or_branch_without_changes(
    database, tmp_path, kind
):
    configured = services(database, tmp_path)
    import_root = configured.settings.repository_import_root
    target = import_root / "existing"
    repository_at(target)
    requested = str(target)
    branch = "main"
    if kind in {"outside", "symlink_outside"}:
        outside = tmp_path / "outside" / "existing"
        repository_at(outside)
        if kind == "symlink_outside":
            link = import_root / "link"
            link.symlink_to(outside, target_is_directory=True)
            requested = str(link)
        else:
            requested = str(outside)
    elif kind == "not_git":
        plain = import_root / "plain"
        plain.mkdir()
        requested = str(plain)
    elif kind == "subdirectory":
        nested = target / "nested"
        nested.mkdir()
        requested = str(nested)
    else:
        branch = "does-not-exist"

    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={"name": "Invalid", "source_path": requested, "default_branch": branch},
        )

    assert response.status_code == 422, response.text
    assert git(target, "status", "--porcelain") == ""
    await assert_no_records(database)


async def test_uploaded_import_contains_only_uploaded_files_and_valid_baseline(database, tmp_path):
    configured = services(database, tmp_path)
    files = [
        {"path": "README.md", "content": "Uploaded project\n"},
        {"path": "lib/subtract.py", "content": "def subtract(a, b):\n    return a - b\n"},
    ]
    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={
                "name": "Uploaded",
                "folder_name": "uploaded",
                "default_branch": "dev",
                "files": files,
            },
        )

    assert response.status_code == 201, response.text
    target = configured.settings.repository_import_root / "uploaded"
    assert git(target, "branch", "--show-current") == "dev"
    assert response.json()["baseline_commit"] == git(target, "rev-parse", "HEAD")
    assert git(target, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        "README.md",
        "lib/subtract.py",
    ]
    assert git(target, "status", "--porcelain") == ""
    for item in files:
        assert (target / item["path"]).read_text() == item["content"]


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md", "README.md"],
        ["README.md", "readme.md"],
        ["src", "src/app.py"],
        ["src/app.py", "src"],
        ["safe.py", "..\\escape.py"],
        ["safe.py", "C:\\absolute.py"],
        ["safe.py", "nested/.git/config"],
        ["safe.py", ".Git/config"],
    ],
)
async def test_uploaded_paths_are_validated_as_a_batch_before_creating_target(
    database, tmp_path, paths
):
    configured = services(database, tmp_path)
    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={
                "name": "Invalid upload",
                "folder_name": "uploaded",
                "files": [{"path": path, "content": "Content"} for path in paths],
            },
        )

    assert response.status_code == 422, response.text
    assert not (configured.settings.repository_import_root / "uploaded").exists()
    await assert_no_records(database)


@pytest.mark.parametrize("branch", ["--config=bad", "bad..branch", "refs/heads/../bad"])
async def test_uploaded_invalid_branch_is_rejected_before_directory_creation(
    database, tmp_path, branch
):
    configured = services(database, tmp_path)
    async with client_for(configured) as client:
        response = await client.post(
            "/api/v1/projects/onboard",
            json={
                "name": "Invalid branch",
                "folder_name": "uploaded",
                "default_branch": branch,
                "files": [{"path": "README.md", "content": "Content"}],
            },
        )

    assert response.status_code == 422, response.text
    assert not (configured.settings.repository_import_root / "uploaded").exists()
    await assert_no_records(database)


@pytest.mark.parametrize("upload", [False, True])
async def test_database_failure_removes_only_new_uploads(database, tmp_path, monkeypatch, upload):
    configured = services(database, tmp_path)
    target = configured.settings.repository_import_root / "target"
    if not upload:
        baseline = repository_at(target)
    payload = {"name": "Database failure", "folder_name": "target"}
    if upload:
        payload["files"] = [{"path": "README.md", "content": "New upload"}]

    async def fail_repository(*args, **kwargs):
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(configured.database_repository, "create_repository", fail_repository)
    async with client_for(configured) as client:
        response = await client.post("/api/v1/projects/onboard", json=payload)

    assert response.status_code == 500
    if upload:
        assert not target.exists()
    else:
        assert target.exists()
        assert git(target, "rev-parse", "HEAD") == baseline
        assert git(target, "status", "--porcelain") == ""
    await assert_no_records(database)


async def test_duplicate_project_id_cleans_new_upload_and_keeps_existing_repository(
    database, tmp_path
):
    configured = services(database, tmp_path)
    target = configured.settings.repository_import_root / "existing"
    repository_at(target)
    project_id = str(uuid4())
    async with client_for(configured) as client:
        first = await client.post(
            "/api/v1/projects/onboard",
            json={"name": "Existing", "folder_name": "existing", "project_id": project_id},
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/v1/projects/onboard",
            json={
                "name": "Duplicate",
                "folder_name": "uploaded",
                "project_id": project_id,
                "files": [{"path": "README.md", "content": "Duplicate"}],
            },
        )

    assert second.status_code == 409, second.text
    assert not (configured.settings.repository_import_root / "uploaded").exists()
    assert git(target, "status", "--porcelain") == ""
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1
        assert await session.scalar(select(func.count()).select_from(RepositoryRow)) == 1
