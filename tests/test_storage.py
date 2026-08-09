import pytest
import os
import tempfile
from autoswe.storage import StorageEngine
from autoswe.models import TaskStatus, RiskLevel


@pytest.fixture
def temp_storage(tmp_path):
    db_path = str(tmp_path / "test_autoswe.db")
    storage_dir = str(tmp_path / "artifacts")
    engine = StorageEngine(db_path=db_path, storage_dir=storage_dir)
    return engine


def test_create_and_get_project(temp_storage):
    project = temp_storage.create_project(
        project_id="proj-1",
        name="Test Project",
        description="A project for unit testing",
        metadata={"owner": "alice"},
    )
    assert project["id"] == "proj-1"
    assert project["name"] == "Test Project"
    assert project["description"] == "A project for unit testing"
    assert project["metadata"] == {"owner": "alice"}


def test_create_and_get_task(temp_storage):
    temp_storage.create_project(project_id="proj-1", name="Test Project")

    task = temp_storage.create_task(
        task_id="task-101",
        project_id="proj-1",
        title="Setup DB Schema",
        description="Create tables",
        assigned_agent="db_agent",
        dependencies=[],
        risk_level=RiskLevel.LOW,
        metadata={"priority": "high"},
    )
    assert task["id"] == "task-101"
    assert task["project_id"] == "proj-1"
    assert task["title"] == "Setup DB Schema"
    assert task["status"] == TaskStatus.PENDING

    fetched = temp_storage.get_task("task-101")
    assert fetched is not None
    assert fetched["id"] == "task-101"
    assert fetched["title"] == "Setup DB Schema"
    assert fetched["assigned_agent"] == "db_agent"
    assert fetched["metadata"] == {"priority": "high"}

    non_existent = temp_storage.get_task("non-existent")
    assert non_existent is None


def test_update_task_state(temp_storage):
    temp_storage.create_project(project_id="proj-1", name="Test Project")
    temp_storage.create_task(
        task_id="task-102",
        project_id="proj-1",
        title="Implement API",
    )

    updated = temp_storage.update_task_state(
        task_id="task-102",
        status=TaskStatus.IN_PROGRESS,
        metadata={"started_by": "worker-1"},
    )
    assert updated is not None
    assert updated["status"] == TaskStatus.IN_PROGRESS
    assert updated["metadata"]["started_by"] == "worker-1"

    fetched = temp_storage.get_task("task-102")
    assert fetched["status"] == TaskStatus.IN_PROGRESS
    assert fetched["metadata"]["started_by"] == "worker-1"


def test_save_and_read_artifact(temp_storage):
    content = "hello world artifact content"
    saved_path = temp_storage.save_artifact("reports/summary.txt", content)
    assert os.path.exists(saved_path)

    read_back = temp_storage.read_artifact("reports/summary.txt")
    assert read_back == content

    binary_content = b"\x00\x01\x02\x03\x04"
    temp_storage.save_artifact("bin/data.bin", binary_content)
    read_binary = temp_storage.read_artifact("bin/data.bin", is_binary=True)
    assert read_binary == binary_content


def test_save_and_get_idempotency_record(temp_storage):
    key = "op-999"
    result_data = {"status": "success", "inserted_id": 42}
    record = temp_storage.save_idempotency_record(key=key, result=result_data)
    assert record["key"] == key
    assert record["result"] == result_data
    assert record["status"] == "completed"

    fetched = temp_storage.get_idempotency_record(key)
    assert fetched is not None
    assert fetched["key"] == key
    assert fetched["result"] == result_data

    assert temp_storage.get_idempotency_record("missing-key") is None


def test_log_audit_event(temp_storage):
    event = temp_storage.log_audit_event(
        event_type="TASK_CREATED",
        actor="system",
        payload={"task_id": "task-101"},
    )
    assert event["event_type"] == "TASK_CREATED"
    assert event["actor"] == "system"
    assert event["payload"] == {"task_id": "task-101"}
    assert "timestamp" in event
