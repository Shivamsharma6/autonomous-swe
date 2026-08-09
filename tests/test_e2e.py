import os
import pytest
from fastapi.testclient import TestClient

from autoswe.control_plane import app, storage as cp_storage
from autoswe.models import TaskStatus
from autoswe.orchestrator import WorkflowOrchestrator
from autoswe.storage import StorageEngine


@pytest.fixture
def e2e_env(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    db_path = str(tmp_path / "e2e_autoswe.db")
    artifact_dir = str(tmp_path / "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)

    storage = StorageEngine(db_path=db_path, storage_dir=artifact_dir)
    orchestrator = WorkflowOrchestrator(storage_engine=storage, workspace_path=workspace)

    # Configure control plane storage to use the same isolated storage
    cp_storage.db_path = db_path
    cp_storage.storage_dir = artifact_dir
    cp_storage.artifact_dir = artifact_dir
    cp_storage._init_db()

    client = TestClient(app)
    return {
        "storage": storage,
        "workspace": workspace,
        "orchestrator": orchestrator,
        "artifact_dir": artifact_dir,
        "client": client,
    }


def test_full_sdlc_platform_e2e_integration(e2e_env):
    storage = e2e_env["storage"]
    workspace = e2e_env["workspace"]
    orchestrator = e2e_env["orchestrator"]
    client = e2e_env["client"]

    # 1. Project Registration
    proj_req = {
        "name": "E2E Platform Test Project",
        "repo_path": workspace,
        "description": "End-to-end multi-agent SDLC workflow project",
        "project_id": "proj-e2e-001",
    }
    proj_res = client.post("/api/v1/projects", json=proj_req)
    assert proj_res.status_code == 200
    proj_data = proj_res.json()
    assert proj_data["project_id"] == "proj-e2e-001"
    assert proj_data["name"] == "E2E Platform Test Project"
    assert proj_data["status"] == "created"

    # Audit log project registration event
    storage.log_audit_event(
        event_type="project_registered",
        actor="e2e_test_user",
        payload={"project_id": "proj-e2e-001", "name": proj_data["name"]},
    )

    # 2. Task Submission
    task_req = {
        "project_id": "proj-e2e-001",
        "user_request": "Implement math helper multiply(a, b) in math_utils.py",
        "description": "Create math_utils.py with multiply function and test_math_utils.py with unit test",
        "task_id": "task-e2e-001",
    }
    task_res = client.post("/api/v1/tasks", json=task_req)
    assert task_res.status_code == 200
    task_data = task_res.json()
    assert task_data["task_id"] == "task-e2e-001"
    assert task_data["project_id"] == "proj-e2e-001"
    assert task_data["status"] == "PENDING"

    # Audit log task submission event
    storage.log_audit_event(
        event_type="task_submitted",
        actor="e2e_test_user",
        payload={"task_id": "task-e2e-001", "user_request": task_req["user_request"]},
    )

    # Verify task retrieval before workflow execution
    get_task_res = client.get("/api/v1/tasks/task-e2e-001")
    assert get_task_res.status_code == 200
    task_info = get_task_res.json()
    assert task_info["id"] == "task-e2e-001"
    assert task_info["status"].upper() == "PENDING"

    # 3. WorkflowOrchestrator Execution
    initial_code = {
        "math_utils.py": "def multiply(a, b):\n    return a * b\n"
    }
    initial_tests = {
        "test_math_utils.py": (
            "from math_utils import multiply\n\n"
            "def test_multiply():\n"
            "    assert multiply(3, 4) == 12\n"
        )
    }

    storage.log_audit_event(
        event_type="workflow_started",
        actor="WorkflowOrchestrator",
        payload={"task_id": "task-e2e-001", "project_id": "proj-e2e-001"},
    )

    result_state = orchestrator.run_workflow(
        user_request=task_req["user_request"],
        project_id="proj-e2e-001",
        initial_code=initial_code,
        initial_tests=initial_tests,
    )

    # Validate state returned by WorkflowOrchestrator
    assert result_state is not None
    assert result_state["workflow_status"] in ("COMPLETED", TaskStatus.COMPLETED)
    assert result_state["current_node"] in ("Final Reviewer", "Final_Reviewer", "COMPLETED")
    assert "dag_state" in result_state
    assert len(result_state["dag_state"]) >= 3
    assert result_state["user_request"] == task_req["user_request"]

    # 4. Sandbox Run Verification
    test_file_path = os.path.join(workspace, "test_math_utils.py")
    assert os.path.exists(test_file_path)
    sandbox_res = orchestrator.sandbox.run_command("python3 -m pytest test_math_utils.py")
    assert sandbox_res["exit_code"] == 0

    # 5. Artifact Log Creation
    assert "artifact_references" in result_state
    assert "log_uri" in result_state["artifact_references"]
    log_uri = result_state["artifact_references"]["log_uri"]
    log_filename = os.path.basename(log_uri)

    log_content = storage.read_artifact(log_filename)
    assert "=== WORKFLOW EXECUTION LOG ===" in log_content
    assert result_state["workflow_id"] in log_content
    assert task_req["user_request"] in log_content
    assert "Exit Code: 0" in log_content

    # 6. Audit Logging Verification
    storage.log_audit_event(
        event_type="workflow_completed",
        actor="WorkflowOrchestrator",
        payload={
            "task_id": result_state["task_id"],
            "workflow_id": result_state["workflow_id"],
            "status": result_state["workflow_status"],
            "log_uri": log_uri,
        },
    )

    # Query database to confirm audit logs were persisted correctly
    with storage._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY id ASC")
        audit_rows = [dict(row) for row in cursor.fetchall()]

    assert len(audit_rows) >= 3
    event_types = [r["event_type"] for r in audit_rows]
    assert "project_registered" in event_types
    assert "task_submitted" in event_types
    assert "workflow_completed" in event_types

    # Verify task state in storage and Control Plane API after completion
    updated_task_res = client.get(f"/api/v1/tasks/{result_state['task_id']}")
    assert updated_task_res.status_code == 200
    updated_task_info = updated_task_res.json()
    assert updated_task_info["status"].upper() == "COMPLETED"


def test_e2e_auto_generation_and_self_healing_workflow(tmp_path):
    workspace = str(tmp_path / "workspace_auto")
    os.makedirs(workspace, exist_ok=True)
    db_path = str(tmp_path / "e2e_auto.db")
    artifact_dir = str(tmp_path / "artifacts_auto")
    os.makedirs(artifact_dir, exist_ok=True)

    storage = StorageEngine(db_path=db_path, storage_dir=artifact_dir)
    orchestrator = WorkflowOrchestrator(storage_engine=storage, workspace_path=workspace)

    # Run workflow without pre-defined initial code/tests (auto-generates code and tests)
    result_state = orchestrator.run_workflow(
        user_request="Build calculate_discount utility module",
        project_id="proj-auto-001",
    )

    assert result_state["workflow_status"] in ("COMPLETED", TaskStatus.COMPLETED)
    assert os.path.exists(os.path.join(workspace, "utils.py"))
    assert os.path.exists(os.path.join(workspace, "test_utils.py"))
    assert "log_uri" in result_state["artifact_references"]

    log_filename = os.path.basename(result_state["artifact_references"]["log_uri"])
    log_content = storage.read_artifact(log_filename)
    assert len(log_content) > 0
