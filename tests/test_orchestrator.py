import os
import pytest
from knowledge.memory.storage import StorageEngine
from workflows.feature import WorkflowOrchestrator
from execution.scheduler.scheduler import TaskStatus


@pytest.fixture
def test_env(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    db_path = str(tmp_path / "test_orch.db")
    artifact_dir = str(tmp_path / "artifacts")

    storage = StorageEngine(db_path=db_path, storage_dir=artifact_dir)
    storage.create_project(project_id="default_project", name="Default Project")
    orchestrator = WorkflowOrchestrator(storage_engine=storage, workspace_path=workspace)
    return {
        "storage": storage,
        "workspace": workspace,
        "orchestrator": orchestrator,
        "artifact_dir": artifact_dir,
    }


def test_workflow_orchestrator_successful_flow(test_env):
    orch = test_env["orchestrator"]
    storage = test_env["storage"]
    workspace = test_env["workspace"]

    code_content = "def calculate_discount(price, rate):\n    return price * (1.0 - rate)\n"
    test_content = (
        "from utils import calculate_discount\n\n"
        "def test_calculate_discount():\n"
        "    assert calculate_discount(100.0, 0.2) == 80.0\n"
    )

    result_state = orch.run_workflow(
        user_request="Create a helper function calculate_discount(price, rate) in utils.py",
        initial_code={"utils.py": code_content},
        initial_tests={"test_utils.py": test_content},
    )

    assert result_state is not None
    assert result_state["workflow_status"] in ("COMPLETED", TaskStatus.COMPLETED)
    assert result_state["current_node"] in ("Final Reviewer", "Final_Reviewer", "COMPLETED")
    assert "dag_state" in result_state
    assert len(result_state["dag_state"]) >= 3
    assert "artifact_references" in result_state
    assert "log_uri" in result_state["artifact_references"]

    log_uri = result_state["artifact_references"]["log_uri"]
    log_content = storage.read_artifact(os.path.basename(log_uri))
    assert len(log_content) > 0

    task_id = result_state["task_id"]
    saved_task = storage.get_task(task_id)
    assert saved_task is not None
    assert saved_task["status"] == TaskStatus.COMPLETED.value or saved_task["status"] == "COMPLETED"


def test_workflow_orchestrator_self_healing_debug_loop(test_env):
    orch = test_env["orchestrator"]
    storage = test_env["storage"]

    flawed_code = "def calculate_discount(price, rate):\n    return price * rate\n"
    test_content = (
        "from utils import calculate_discount\n\n"
        "def test_calculate_discount():\n"
        "    assert calculate_discount(100.0, 0.2) == 80.0\n"
    )
    fixed_code = "def calculate_discount(price, rate):\n    return price * (1.0 - rate)\n"

    def self_healing_fix_handler(workspace_path, stack_trace):
        utils_path = os.path.join(workspace_path, "utils.py")
        with open(utils_path, "w", encoding="utf-8") as f:
            f.write(fixed_code)

    result_state = orch.run_workflow(
        user_request="Fix calculate_discount logic in utils.py",
        initial_code={"utils.py": flawed_code},
        initial_tests={"test_utils.py": test_content},
        debug_fix_handler=self_healing_fix_handler,
    )

    assert result_state["workflow_status"] in ("COMPLETED", TaskStatus.COMPLETED)
    assert result_state["retry_count"] >= 1
    assert "log_uri" in result_state["artifact_references"]


def test_workflow_orchestrator_architect_researcher_coder_tester_steps(test_env):
    orch = test_env["orchestrator"]

    result_state = orch.run_workflow(
        user_request="Build math helper module",
    )

    assert "dag_state" in result_state
    assert "task_id" in result_state
    assert "workflow_id" in result_state
    assert result_state["workflow_status"] in ("COMPLETED", "FAILED", TaskStatus.COMPLETED, TaskStatus.FAILED)
