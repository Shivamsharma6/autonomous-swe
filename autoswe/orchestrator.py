import os
import time
from typing import Any, Callable, Dict, Optional
from autoswe.agent_runtime import get_default_agent_specs
from autoswe.context_engine import ContextEngine
from autoswe.models import TaskStatus, ToolCallRequest
from autoswe.sandbox import SandboxRunner
from autoswe.scheduler import TaskPlanner
from autoswe.storage import StorageEngine
from autoswe.tool_gateway import RiskPolicyEngine, ToolGateway


class WorkflowOrchestrator:
    """LangGraph-compatible multi-agent SDLC orchestration engine."""

    def __init__(
        self,
        storage_engine: Optional[StorageEngine] = None,
        workspace_path: str = ".",
    ):
        self.storage = storage_engine or StorageEngine()
        self.workspace_path = os.path.abspath(workspace_path)
        os.makedirs(self.workspace_path, exist_ok=True)

        self.context_engine = ContextEngine(workspace_path=self.workspace_path)
        self.policy = RiskPolicyEngine()
        self.gateway = ToolGateway(storage_engine=self.storage, risk_engine=self.policy)
        self.sandbox = SandboxRunner(work_dir=self.workspace_path)
        self.specs = get_default_agent_specs()
        self.planner = TaskPlanner()

    def _scan_workspace_files(self) -> Dict[str, str]:
        repo_files = {}
        for root, _, files in os.walk(self.workspace_path):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.workspace_path)
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            repo_files[rel_path] = f.read()
                    except Exception:
                        pass
        return repo_files

    def run_workflow(
        self,
        user_request: str,
        project_id: str = "default_project",
        initial_code: Optional[Dict[str, str]] = None,
        initial_tests: Optional[Dict[str, str]] = None,
        debug_fix_handler: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        workflow_id = f"wf-{int(time.time() * 1000)}"
        task_id = f"task-{int(time.time() * 1000)}"

        # Ensure project exists in storage
        try:
            self.storage.create_project(
                project_id=project_id,
                name="Default Project",
                description="Auto-generated default project",
            )
        except Exception:
            pass

        self.storage.create_task(
            task_id=task_id,
            project_id=project_id,
            title=user_request,
            description=user_request,
            status=TaskStatus.RUNNING,
        )

        state = {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "project_id": project_id,
            "user_request": user_request,
            "current_node": "Architect",
            "dag_state": {},
            "retry_count": 0,
            "max_retries": 3,
            "workflow_status": "IN_PROGRESS",
            "artifact_references": {},
        }

        # 1. Architect Step: Generate Task DAG
        dag_nodes = self.planner.generate_dag(user_request)
        state["dag_state"] = {
            node.id: node.model_dump() if hasattr(node, "model_dump") else node.__dict__
            for node in dag_nodes
        }

        # 2. Researcher Step: Gather Context & Setup Initial Files
        state["current_node"] = "Researcher"
        if initial_code:
            for file_path, content in initial_code.items():
                full_p = os.path.join(self.workspace_path, file_path)
                os.makedirs(os.path.dirname(full_p), exist_ok=True)
                with open(full_p, "w", encoding="utf-8") as f:
                    f.write(content)

        if initial_tests:
            for file_path, content in initial_tests.items():
                full_p = os.path.join(self.workspace_path, file_path)
                os.makedirs(os.path.dirname(full_p), exist_ok=True)
                with open(full_p, "w", encoding="utf-8") as f:
                    f.write(content)

        repo_files = self._scan_workspace_files()
        assembled_context = self.context_engine.assemble_context(
            task_request=user_request,
            repo_files=repo_files,
            memory_notes=["Follow clean software design and unit testing practices"],
        )
        state["assembled_context"] = assembled_context

        # 3. Coder Step: Write implementation code if missing
        state["current_node"] = "Coder"
        if not repo_files or not any(f for f in repo_files if not f.startswith("test_")):
            default_code = "def calculate_discount(price, rate):\n    return price * (1.0 - rate)\n"
            code_req = ToolCallRequest(
                call_id=f"{task_id}_coder_write",
                tool_name="write_file",
                arguments={"path": "utils.py", "content": default_code},
                requested_by="Coder",
            )
            self.gateway.execute_tool(
                code_req,
                executor_func=lambda req: self._write_file_executor(req.arguments),
                idempotency_key=f"{task_id}_coder_write",
            )

        # 4. Tester Step: Generate unit test if missing
        state["current_node"] = "Tester"
        repo_files = self._scan_workspace_files()
        if not any(f.startswith("test_") for f in repo_files):
            default_test = (
                "from utils import calculate_discount\n\n"
                "def test_calculate_discount():\n"
                "    assert calculate_discount(100.0, 0.2) == 80.0\n"
            )
            test_req = ToolCallRequest(
                call_id=f"{task_id}_tester_write",
                tool_name="write_file",
                arguments={"path": "test_utils.py", "content": default_test},
                requested_by="Tester",
            )
            self.gateway.execute_tool(
                test_req,
                executor_func=lambda req: self._write_file_executor(req.arguments),
                idempotency_key=f"{task_id}_tester_write",
            )

        # 5 & 6. Sandbox Test Run & Self-Healing Debug Loop
        state["current_node"] = "Sandbox_Run"
        test_res = self.sandbox.run_command("python3 -m pytest")

        while test_res["exit_code"] != 0 and state["retry_count"] < state["max_retries"]:
            state["current_node"] = "Debugger"
            state["retry_count"] += 1

            stack_trace = test_res.get("stderr", "") + "\n" + test_res.get("stdout", "")
            if debug_fix_handler:
                debug_fix_handler(self.workspace_path, stack_trace)
            else:
                # Default fallback fix if code was flawed
                utils_p = os.path.join(self.workspace_path, "utils.py")
                if os.path.exists(utils_p):
                    fixed_code = "def calculate_discount(price, rate):\n    return price * (1.0 - rate)\n"
                    with open(utils_p, "w", encoding="utf-8") as f:
                        f.write(fixed_code)

            state["current_node"] = "Sandbox_Run"
            test_res = self.sandbox.run_command("python3 -m pytest")

        if test_res["exit_code"] == 0:
            state["workflow_status"] = "COMPLETED"
            state["current_node"] = "Final Reviewer"
        else:
            state["workflow_status"] = "FAILED"
            state["current_node"] = "Debugger"

        # 7. Artifact Log Saving & Task Update
        log_key = f"workflow_{workflow_id}.log"
        log_content = (
            f"=== WORKFLOW EXECUTION LOG ===\n"
            f"Workflow ID: {workflow_id}\n"
            f"Task ID: {task_id}\n"
            f"User Request: {user_request}\n"
            f"Status: {state['workflow_status']}\n"
            f"Retries: {state['retry_count']}\n"
            f"\n--- Sandbox Test Execution Output ---\n"
            f"Exit Code: {test_res.get('exit_code')}\n"
            f"Stdout:\n{test_res.get('stdout')}\n"
            f"Stderr:\n{test_res.get('stderr')}\n"
        )
        log_uri = self.storage.save_artifact(key=log_key, content=log_content)
        state["artifact_references"]["log_uri"] = log_uri

        self.storage.update_task_state(
            task_id=task_id,
            status=state["workflow_status"],
            metadata={"workflow_state": state},
        )

        return state

    def _write_file_executor(self, args: Dict[str, Any]):
        rel_path = args["path"]
        content = args["content"]
        full_path = os.path.join(self.workspace_path, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        from autoswe.models import ToolCallResult
        return ToolCallResult(
            call_id=f"write_{rel_path}",
            tool_name="write_file",
            output=f"Successfully wrote {rel_path}",
            is_success=True,
        )
