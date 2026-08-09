# Autonomous Software Engineering Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-grade Autonomous Software Engineering Platform with LangGraph multi-agent orchestration, first-class task DAG scheduler with worker leases/heartbeats, 4-dimensional ContextEngine, dynamic risk policy engine with idempotency keys, and a modern WebSocket-driven web dashboard.

**Architecture:** A modular Python core package (`autoswe/`) providing data models, storage managers, context builder, declarative agent runtime, sandboxed tool gateway, first-class task scheduler, and FastAPI control plane server, accompanied by a rich single-page web dashboard UI (`frontend/`).

**Tech Stack:** Python 3.11+, LangGraph, LangChain, FastAPI, Uvicorn, Pydantic, SQLite/PostgreSQL, Redis (optional pub/sub), Docker/subprocess sandbox, Vanilla CSS/JS HTML5 SPA dashboard.

---

## Proposed Directory & File Layout

```
/Users/shivamsharma/projects/autonomous-swe/
├── autoswe/
│   ├── __init__.py
│   ├── models.py             # WorkflowState, TaskNode, AgentSpec, RiskLevel, IdempotencyRecord
│   ├── storage.py            # System of record metadata DB & Object/File artifact store
│   ├── context_engine.py     # 4D ContextEngine (Repo, Task, Memory, Execution) & ContextBuilder
│   ├── agent_runtime.py      # Declarative AgentSpec & AgentRuntime execution lifecycle
│   ├── tool_gateway.py       # RiskPolicyEngine, secret redactor, audit logger, idempotency key check
│   ├── sandbox.py            # Docker container & subprocess sandbox runner
│   ├── scheduler.py          # TaskPlanner (DAG) & TaskScheduler (Leases, Heartbeats, DLQ, Cancel)
│   ├── orchestrator.py       # LangGraph StateGraph engine & self-healing debug loop
│   └── control_plane.py      # FastAPI REST server & WebSocket streaming hub
├── frontend/
│   ├── index.html            # Single-Page Dashboard UI structure
│   ├── styles.css            # Dark mode glassmorphism theme & CSS grid layout
│   └── app.js                # WebSocket client, DAG renderer, diff inspector, approval modal
├── tests/
│   ├── test_models.py
│   ├── test_storage.py
│   ├── test_context_engine.py
│   ├── test_tool_gateway.py
│   ├── test_scheduler.py
│   ├── test_orchestrator.py
│   └── test_control_plane.py
├── pyproject.toml
└── README.md
```

---

### Task 1: Setup Workspace & Core Data Models (`autoswe/models.py`)

**Files:**
- Create: `pyproject.toml`
- Create: `autoswe/__init__.py`
- Create: `autoswe/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Create `pyproject.toml` dependencies**

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "autonomous-swe"
version = "0.1.0"
description = "Autonomous Software Engineering Platform — Multi-Agent SDLC Orchestration"
dependencies = [
    "fastapi>=0.100.0",
    "uvicorn>=0.22.0",
    "pydantic>=2.0.0",
    "langgraph>=0.1.0",
    "langchain>=0.2.0",
    "langchain-core>=0.2.0",
    "pytest>=7.0.0",
    "websockets>=11.0"
]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
```

- [ ] **Step 2: Write failing unit test for core models**

```python
# tests/test_models.py
from autoswe.models import (
    WorkflowState, TaskNode, TaskStatus, AgentSpec, RiskLevel,
    ToolCallRequest, ToolCallResult, IdempotencyRecord
)

def test_workflow_state_initialization():
    state = WorkflowState(
        workflow_id="wf-123",
        task_id="task-456",
        project_id="proj-789",
        user_request="Add authentication endpoint",
        current_node="Architect",
        dag_state={},
        retry_budget_state={"retry_count": 0, "max_retries": 3, "budget_consumed_usd": 0.0, "budget_cap_usd": 2.0},
        approval_state={"pending_gate_id": None, "status": "APPROVED"},
        artifact_references={}
    )
    assert state.workflow_id == "wf-123"
    assert state.retry_budget_state["max_retries"] == 3

def test_agent_spec_definition():
    spec = AgentSpec(
        role="Coder",
        description="Writes feature code",
        model_policy={"primary_model": "gemini-3.6-flash"},
        tools=["write_file", "read_file"],
        permissions=["WORKSPACE_EDIT"],
        context_policy={"token_budget": 8000},
        budget={"max_cost_usd": 1.0, "max_time_sec": 120},
        termination_policy={"stop_on_success": True}
    )
    assert spec.role == "Coder"
    assert "write_file" in spec.tools
```

- [ ] **Step 3: Run test to verify failure**

Run: `pytest tests/test_models.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe'"

- [ ] **Step 4: Implement core models in `autoswe/models.py`**

```python
# autoswe/__init__.py
__version__ = "0.1.0"

# autoswe/models.py
from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECOVERABLE = "RECOVERABLE"
    CANCELLED = "CANCELLED"

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class TaskNode(BaseModel):
    id: str
    name: str
    assigned_agent: str
    dependencies: List[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    input_artifacts: Dict[str, str] = Field(default_factory=dict)
    output_artifacts: Dict[str, str] = Field(default_factory=dict)

class WorkflowState(BaseModel):
    workflow_id: str
    task_id: str
    project_id: str
    user_request: str
    current_node: str = "START"
    dag_state: Dict[str, Any] = Field(default_factory=dict)
    retry_budget_state: Dict[str, Any] = Field(default_factory=lambda: {
        "retry_count": 0, "max_retries": 3, "budget_consumed_usd": 0.0, "budget_cap_usd": 2.0
    })
    approval_state: Dict[str, Any] = Field(default_factory=lambda: {
        "pending_gate_id": None, "status": "APPROVED"
    })
    artifact_references: Dict[str, str] = Field(default_factory=dict)

class AgentSpec(BaseModel):
    role: str
    description: str
    model_policy: Dict[str, Any] = Field(default_factory=dict)
    tools: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    context_policy: Dict[str, Any] = Field(default_factory=dict)
    budget: Dict[str, Any] = Field(default_factory=dict)
    termination_policy: Dict[str, Any] = Field(default_factory=dict)

class ToolCallRequest(BaseModel):
    idempotency_key: str
    task_id: str
    execution_id: str
    attempt: int = 1
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)

class ToolCallResult(BaseModel):
    idempotency_key: str
    success: bool
    exit_code: int = 0
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    artifact_uri: Optional[str] = None

class IdempotencyRecord(BaseModel):
    key: str
    tool_name: str
    result: ToolCallResult
    created_at: float
```

- [ ] **Step 5: Run unit tests to verify pass**

Run: `pytest tests/test_models.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml autoswe/__init__.py autoswe/models.py tests/test_models.py
git commit -m "feat: setup core project structure and Pydantic models"
```

---

### Task 2: Persistence & Storage Layer (`autoswe/storage.py`)

**Files:**
- Create: `autoswe/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing unit test for storage engine**

```python
# tests/test_storage.py
import pytest
import os
import shutil
from autoswe.storage import StorageEngine
from autoswe.models import ToolCallResult

@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "test_autoswe.db")
    artifact_dir = str(tmp_path / "artifacts")
    engine = StorageEngine(db_path=db_path, artifact_dir=artifact_dir)
    yield engine

def test_project_and_task_crud(storage):
    proj_id = storage.create_project(name="Demo App", repo_path="/tmp/demo")
    assert proj_id is not None
    
    task_id = storage.create_task(project_id=proj_id, user_request="Build login endpoint")
    task = storage.get_task(task_id)
    assert task["user_request"] == "Build login endpoint"
    assert task["status"] == "PENDING"

def test_artifact_offloading(storage):
    uri = storage.save_artifact(filename="patch_01.diff", content="--- a/file.py\n+++ b/file.py")
    assert uri.startswith("file://")
    content = storage.read_artifact(uri)
    assert "--- a/file.py" in content

def test_idempotency_storage(storage):
    res = ToolCallResult(
        idempotency_key="key-123", success=True, exit_code=0, output="OK", duration_ms=12.5
    )
    storage.save_idempotency_record("key-123", "run_test", res)
    rec = storage.get_idempotency_record("key-123")
    assert rec is not None
    assert rec.result.output == "OK"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_storage.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.storage'"

- [ ] **Step 3: Implement `StorageEngine` in `autoswe/storage.py`**

```python
# autoswe/storage.py
import sqlite3
import json
import os
import time
from typing import Dict, Any, Optional, List
from autoswe.models import ToolCallResult, IdempotencyRecord

class StorageEngine:
    def __init__(self, db_path: str = "autoswe.db", artifact_dir: str = ".artifacts"):
        self.db_path = db_path
        self.artifact_dir = os.path.abspath(artifact_dir)
        os.makedirs(self.artifact_dir, exist_ok=True)
        self._init_sqlite()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self):
        with self._get_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                user_request TEXT NOT NULL,
                status TEXT NOT NULL,
                workflow_state TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS idempotency_records (
                key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                agent_role TEXT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT,
                risk_level TEXT,
                exit_code INTEGER,
                duration_ms REAL,
                created_at REAL NOT NULL
            );
            """)

    def create_project(self, name: str, repo_path: str) -> str:
        project_id = f"proj-{int(time.time()*1000)}"
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
                (project_id, name, repo_path, time.time())
            )
        return project_id

    def create_task(self, project_id: str, user_request: str) -> str:
        task_id = f"task-{int(time.time()*1000)}"
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO tasks (id, project_id, user_request, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, project_id, user_request, "PENDING", now, now)
            )
        return task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("workflow_state"):
                res["workflow_state"] = json.loads(res["workflow_state"])
            return res

    def update_task_state(self, task_id: str, status: str, workflow_state: Optional[Dict[str, Any]] = None):
        now = time.time()
        with self._get_conn() as conn:
            state_str = json.dumps(workflow_state) if workflow_state else None
            conn.execute(
                "UPDATE tasks SET status = ?, workflow_state = ?, updated_at = ? WHERE id = ?",
                (status, state_str, now, task_id)
            )

    def save_artifact(self, filename: str, content: str) -> str:
        filepath = os.path.join(self.artifact_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"file://{filepath}"

    def read_artifact(self, uri: str) -> str:
        filepath = uri.replace("file://", "")
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    def save_idempotency_record(self, key: str, tool_name: str, result: ToolCallResult):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_records (key, tool_name, result_json, created_at) VALUES (?, ?, ?, ?)",
                (key, tool_name, result.model_dump_json(), time.time())
            )

    def get_idempotency_record(self, key: str) -> Optional[IdempotencyRecord]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM idempotency_records WHERE key = ?", (key,)).fetchone()
            if not row:
                return None
            res_data = json.loads(row["result_json"])
            result = ToolCallResult(**res_data)
            return IdempotencyRecord(
                key=row["key"], tool_name=row["tool_name"], result=result, created_at=row["created_at"]
            )

    def log_audit_event(self, task_id: str, agent_role: str, tool_name: str, args: Dict[str, Any], risk_level: str, exit_code: int, duration_ms: float):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_logs (task_id, agent_role, tool_name, arguments_json, risk_level, exit_code, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, agent_role, tool_name, json.dumps(args), risk_level, exit_code, duration_ms, time.time())
            )
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_storage.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/storage.py tests/test_storage.py
git commit -m "feat: add SQLite and Object Storage persistence engine"
```

---

### Task 3: Context Engineering Subsystem (`autoswe/context_engine.py`)

**Files:**
- Create: `autoswe/context_engine.py`
- Test: `tests/test_context_engine.py`

- [ ] **Step 1: Write failing unit test for 4D ContextEngine & ContextBuilder**

```python
# tests/test_context_engine.py
from autoswe.context_engine import ContextEngine, ContextBuilder

def test_context_engine_4d_assembly():
    engine = ContextEngine(workspace_path="/tmp")
    ctx = engine.assemble_context(
        task_request="Add login authentication endpoint",
        repo_files={"app/main.py": "def main(): pass"},
        memory_notes=["User prefers FastAPI router pattern"],
        execution_context={"failed_command": "pytest", "stack_trace": "AssertionError at test_auth.py:12"}
    )
    
    assert "Repository Context" in ctx
    assert "Task Context" in ctx
    assert "Memory Context" in ctx
    assert "Execution Context" in ctx
    assert "FastAPI router pattern" in ctx
    assert "AssertionError at test_auth.py:12" in ctx

def test_context_builder_pruning():
    builder = ContextBuilder(token_budget=100)
    huge_text = "Word " * 500
    pruned = builder.prune_text(huge_text, max_chars=200)
    assert len(pruned) <= 220
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_context_engine.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.context_engine'"

- [ ] **Step 3: Implement `ContextEngine` & `ContextBuilder` in `autoswe/context_engine.py`**

```python
# autoswe/context_engine.py
from typing import Dict, Any, List, Optional

class ContextBuilder:
    def __init__(self, token_budget: int = 4000):
        self.token_budget = token_budget

    def prune_text(self, text: str, max_chars: int = 2000) -> str:
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return text[:half] + "\n... [TRUNCATED FOR TOKEN BUDGET] ...\n" + text[-half:]

class ContextEngine:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.builder = ContextBuilder()

    def assemble_context(
        self,
        task_request: str,
        repo_files: Optional[Dict[str, str]] = None,
        memory_notes: Optional[List[str]] = None,
        execution_context: Optional[Dict[str, Any]] = None
    ) -> str:
        sections = []

        # 1. Task Context
        sections.append(f"### Task Context\nGoal: {task_request}\n")

        # 2. Repository Context
        if repo_files:
            file_summaries = []
            for path, content in repo_files.items():
                pruned_content = self.builder.prune_text(content, max_chars=800)
                file_summaries.append(f"File: `{path}`:\n```\n{pruned_content}\n```")
            sections.append("### Repository Context\n" + "\n".join(file_summaries) + "\n")

        # 3. Memory Context
        if memory_notes:
            sections.append("### Memory Context (Past Decisions & Rules)\n" + "\n".join(f"- {m}" for m in memory_notes) + "\n")

        # 4. Execution Context (Critical for Debugging)
        if execution_context:
            exec_str = "### Execution Context (Diffs & Errors)\n"
            if "failed_command" in execution_context:
                exec_str += f"Failed Command: `{execution_context['failed_command']}`\n"
            if "stack_trace" in execution_context:
                exec_str += f"Stack Trace:\n```\n{self.builder.prune_text(execution_context['stack_trace'], 1000)}\n```\n"
            if "current_diff" in execution_context:
                exec_str += f"Current Diff:\n```diff\n{self.builder.prune_text(execution_context['current_diff'], 1000)}\n```\n"
            sections.append(exec_str)

        return "\n".join(sections)
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_context_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/context_engine.py tests/test_context_engine.py
git commit -m "feat: implement 4D ContextEngine and ContextBuilder token pruner"
```

---

### Task 4: Tool Gateway, Risk Policy Engine & Secret Redactor (`autoswe/tool_gateway.py`)

**Files:**
- Create: `autoswe/tool_gateway.py`
- Test: `tests/test_tool_gateway.py`

- [ ] **Step 1: Write failing unit test for ToolGateway & RiskPolicyEngine**

```python
# tests/test_tool_gateway.py
import pytest
from autoswe.tool_gateway import ToolGateway, RiskPolicyEngine
from autoswe.models import RiskLevel, ToolCallRequest
from autoswe.storage import StorageEngine

@pytest.fixture
def gateway(tmp_path):
    db_path = str(tmp_path / "test_gateway.db")
    storage = StorageEngine(db_path=db_path)
    policy = RiskPolicyEngine()
    gw = ToolGateway(storage_engine=storage, risk_policy=policy)
    yield gw

def test_risk_scoring():
    policy = RiskPolicyEngine()
    assert policy.assess_risk("pytest", {}) == RiskLevel.LOW
    assert policy.assess_risk("git_diff", {}) == RiskLevel.LOW
    assert policy.assess_risk("pip_install", {"package": "requests"}) == RiskLevel.MEDIUM
    assert policy.assess_risk("git_push", {"remote": "origin"}) == RiskLevel.HIGH
    assert policy.assess_risk("terraform_apply", {}) == RiskLevel.CRITICAL

def test_secret_redaction(gateway):
    raw_text = "API Key: sk-proj-1234567890abcdef and token ghp_ABC123XYZ secret!"
    clean_text = gateway.redact_secrets(raw_text)
    assert "sk-proj-1234567890abcdef" not in clean_text
    assert "[REDACTED_API_KEY]" in clean_text or "[REDACTED]" in clean_text

def test_idempotent_tool_execution(gateway):
    req = ToolCallRequest(
        idempotency_key="test-key-001",
        task_id="task-1",
        execution_id="exec-1",
        attempt=1,
        tool_name="run_command",
        arguments={"command": "echo Hello"}
    )
    
    # First execution
    res1 = gateway.execute_tool(req, executor_fn=lambda args: "Hello World", agent_role="Coder")
    assert res1.success is True
    assert res1.output == "Hello World"
    
    # Second execution (idempotent cache hit)
    res2 = gateway.execute_tool(req, executor_fn=lambda args: "Should Not Execute", agent_role="Coder")
    assert res2.output == "Hello World"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_tool_gateway.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.tool_gateway'"

- [ ] **Step 3: Implement `ToolGateway` & `RiskPolicyEngine` in `autoswe/tool_gateway.py`**

```python
# autoswe/tool_gateway.py
import re
import time
from typing import Dict, Any, Callable
from autoswe.models import RiskLevel, ToolCallRequest, ToolCallResult
from autoswe.storage import StorageEngine

class RiskPolicyEngine:
    def __init__(self):
        self.rules = {
            "pytest": RiskLevel.LOW,
            "git_diff": RiskLevel.LOW,
            "read_file": RiskLevel.LOW,
            "list_dir": RiskLevel.LOW,
            "write_file": RiskLevel.MEDIUM,
            "pip_install": RiskLevel.MEDIUM,
            "git_commit": RiskLevel.MEDIUM,
            "git_push": RiskLevel.HIGH,
            "deploy": RiskLevel.CRITICAL,
            "terraform_apply": RiskLevel.CRITICAL
        }

    def assess_risk(self, tool_name: str, arguments: Dict[str, Any]) -> RiskLevel:
        if tool_name in self.rules:
            return self.rules[tool_name]
        if "command" in arguments:
            cmd = arguments["command"]
            if any(k in cmd for k in ["pytest", "git diff", "ls", "cat"]):
                return RiskLevel.LOW
            if any(k in cmd for k in ["pip install", "npm install"]):
                return RiskLevel.MEDIUM
            if any(k in cmd for k in ["git push", "rm -rf"]):
                return RiskLevel.HIGH
        return RiskLevel.MEDIUM

class ToolGateway:
    SECRET_PATTERNS = [
        r"sk-[a-zA-Z0-9_-]{20,}",
        r"ghp_[a-zA-Z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"bearer\s+[a-zA-Z0-9_\-\.]+"
    ]

    def __init__(self, storage_engine: StorageEngine, risk_policy: RiskPolicyEngine):
        self.storage = storage_engine
        self.policy = risk_policy

    def redact_secrets(self, text: str) -> str:
        clean = text
        for pat in self.SECRET_PATTERNS:
            clean = re.sub(pat, "[REDACTED_SECRET]", clean, flags=re.IGNORECASE)
        return clean

    def execute_tool(
        self,
        request: ToolCallRequest,
        executor_fn: Callable[[Dict[str, Any]], str],
        agent_role: str = "Unknown"
    ) -> ToolCallResult:
        # 1. Idempotency Check
        cached = self.storage.get_idempotency_record(request.idempotency_key)
        if cached:
            return cached.result

        # 2. Risk Assessment
        risk = self.policy.assess_risk(request.tool_name, request.arguments)

        # 3. Execution
        start_time = time.time()
        success = True
        exit_code = 0
        error_msg = ""
        output = ""

        try:
            raw_out = executor_fn(request.arguments)
            output = self.redact_secrets(str(raw_out))
        except Exception as e:
            success = False
            exit_code = 1
            error_msg = self.redact_secrets(str(e))

        duration_ms = (time.time() - start_time) * 1000.0

        res = ToolCallResult(
            idempotency_key=request.idempotency_key,
            success=success,
            exit_code=exit_code,
            output=output,
            error=error_msg,
            duration_ms=duration_ms
        )

        # 4. Save Audit Log & Idempotency Record
        self.storage.log_audit_event(
            task_id=request.task_id,
            agent_role=agent_role,
            tool_name=request.tool_name,
            args=request.arguments,
            risk_level=risk.value,
            exit_code=exit_code,
            duration_ms=duration_ms
        )
        self.storage.save_idempotency_record(request.idempotency_key, request.tool_name, res)

        return res
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_tool_gateway.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/tool_gateway.py tests/test_tool_gateway.py
git commit -m "feat: implement RiskPolicyEngine, ToolGateway secret redactor & idempotency checking"
```

---

### Task 5: Sandboxed Code Execution Runner (`autoswe/sandbox.py`)

**Files:**
- Create: `autoswe/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write failing unit test for SandboxRunner**

```python
# tests/test_sandbox.py
import pytest
from autoswe.sandbox import SandboxRunner

def test_sandbox_isolated_command_execution(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path))
    res = runner.run_command("python3 -c 'print(10 + 20)'")
    assert res["exit_code"] == 0
    assert "30" in res["stdout"]

def test_sandbox_timeout_enforcement(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path), timeout_sec=1)
    res = runner.run_command("python3 -c 'import time; time.sleep(5)'")
    assert res["exit_code"] != 0
    assert "TIMED_OUT" in res["stderr"] or res["exit_code"] == 124
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_sandbox.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.sandbox'"

- [ ] **Step 3: Implement `SandboxRunner` in `autoswe/sandbox.py`**

```python
# autoswe/sandbox.py
import subprocess
import shlex
import time
import os
from typing import Dict, Any

class SandboxRunner:
    def __init__(self, work_dir: str, timeout_sec: int = 30):
        self.work_dir = os.path.abspath(work_dir)
        self.timeout_sec = timeout_sec

    def run_command(self, command_str: str, env_override: Dict[str, str] = None) -> Dict[str, Any]:
        env = os.environ.copy()
        # Clean sensitive keys from environment
        for key in ["AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
            env.pop(key, None)
        if env_override:
            env.update(env_override)

        start_time = time.time()
        try:
            proc = subprocess.run(
                command_str,
                shell=True,
                cwd=self.work_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec
            )
            duration_ms = (time.time() - start_time) * 1000.0
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "duration_ms": duration_ms
            }
        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000.0
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"TIMED_OUT after {self.timeout_sec}s",
                "duration_ms": duration_ms
            }
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_sandbox.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/sandbox.py tests/test_sandbox.py
git commit -m "feat: add Sandboxed command runner with timeouts and env isolation"
```

---

### Task 6: Declarative Agent Runtime & Specs (`autoswe/agent_runtime.py`)

**Files:**
- Create: `autoswe/agent_runtime.py`
- Test: `tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing unit test for AgentRuntime**

```python
# tests/test_agent_runtime.py
from autoswe.agent_runtime import AgentRuntime, get_default_agent_specs
from autoswe.models import AgentSpec

def test_default_agent_specs():
    specs = get_default_agent_specs()
    assert "Architect" in specs
    assert "Coder" in specs
    assert "Tester" in specs
    assert "Debugger" in specs
    assert "Final Reviewer" in specs

def test_agent_runtime_invocation():
    spec = get_default_agent_specs()["Architect"]
    runtime = AgentRuntime(spec=spec)
    prompt = runtime.build_agent_prompt(task_goal="Create user authentication API")
    assert "Architect" in prompt
    assert "Create user authentication API" in prompt
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_agent_runtime.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.agent_runtime'"

- [ ] **Step 3: Implement `AgentRuntime` and default specs in `autoswe/agent_runtime.py`**

```python
# autoswe/agent_runtime.py
from typing import Dict, Any
from autoswe.models import AgentSpec

def get_default_agent_specs() -> Dict[str, AgentSpec]:
    return {
        "Architect": AgentSpec(
            role="Architect",
            description="Decomposes requirements into structured task DAGs",
            tools=["list_dir", "read_file"],
            permissions=["READ_ONLY"],
            context_policy={"token_budget": 4000}
        ),
        "Researcher": AgentSpec(
            role="Researcher",
            description="Indexes codebase AST and retrieves relevant context",
            tools=["search_code", "read_file"],
            permissions=["READ_ONLY"],
            context_policy={"token_budget": 4000}
        ),
        "Coder": AgentSpec(
            role="Coder",
            description="Writes code feature implementations and updates files",
            tools=["write_file", "read_file"],
            permissions=["WORKSPACE_EDIT"],
            context_policy={"token_budget": 6000}
        ),
        "Tester": AgentSpec(
            role="Test Generator",
            description="Generates comprehensive pytest unit tests and mocks",
            tools=["write_file", "read_file"],
            permissions=["WORKSPACE_EDIT"],
            context_policy={"token_budget": 4000}
        ),
        "Reviewer": AgentSpec(
            role="Reviewer",
            description="Evaluates code quality, lint status, and security compliance",
            tools=["read_file", "pytest"],
            permissions=["READ_ONLY"],
            context_policy={"token_budget": 4000}
        ),
        "Debugger": AgentSpec(
            role="Debugger",
            description="Parses stack traces and implements self-healing code fixes",
            tools=["write_file", "read_file", "pytest"],
            permissions=["WORKSPACE_EDIT"],
            context_policy={"token_budget": 6000}
        ),
        "Final Reviewer": AgentSpec(
            role="Final Reviewer",
            description="Evaluates completed feature and prepares Git Pull Request",
            tools=["git_diff", "git_commit"],
            permissions=["WORKSPACE_EDIT"],
            context_policy={"token_budget": 4000}
        )
    }

class AgentRuntime:
    def __init__(self, spec: AgentSpec):
        self.spec = spec

    def build_agent_prompt(self, task_goal: str, assembled_context: str = "") -> str:
        return (
            f"System: You are the {self.spec.role} Agent.\n"
            f"Role Description: {self.spec.description}\n"
            f"Allowed Tools: {', '.join(self.spec.tools)}\n\n"
            f"User Goal: {task_goal}\n\n"
            f"{assembled_context}"
        )
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_agent_runtime.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/agent_runtime.py tests/test_agent_runtime.py
git commit -m "feat: implement declarative AgentSpec and AgentRuntime"
```

---

### Task 7: First-Class Task Planner & Scheduler (`autoswe/scheduler.py`)

**Files:**
- Create: `autoswe/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing unit test for TaskScheduler with worker leases & heartbeats**

```python
# tests/test_scheduler.py
import time

from autoswe.models import TaskNode, TaskStatus
from autoswe.scheduler import TaskPlanner, TaskScheduler


def test_task_planner_dag_generation():
  planner = TaskPlanner()
  nodes = planner.generate_dag("Implement user authentication")
  assert len(nodes) >= 3
  assert nodes[0].assigned_agent == "Researcher"
  assert nodes[1].dependencies == [nodes[0].id]


def test_task_scheduler_leasing_and_heartbeats():
  scheduler = TaskScheduler(lease_ttl_sec=1)
  n1 = TaskNode(id="t1", name="Task 1", assigned_agent="Coder")
  scheduler.register_task(n1)

  # Check initial status
  ready = scheduler.get_ready_tasks()
  assert len(ready) == 1
  assert ready[0].id == "t1"

  # Lease task
  lease = scheduler.lease_task("t1", worker_id="w-101")
  assert lease is not None
  assert scheduler.get_task_status("t1") == TaskStatus.LEASED

  # Worker heartbeat
  assert scheduler.send_heartbeat("t1", worker_id="w-101") is True

  # Wait for lease expiration without heartbeat
  time.sleep(1.2)
  scheduler.reclaim_expired_leases()
  assert scheduler.get_task_status("t1") == TaskStatus.READY


def test_task_cancellation():
  scheduler = TaskScheduler()
  n1 = TaskNode(id="t1", name="Task 1", assigned_agent="Coder")
  scheduler.register_task(n1)
  scheduler.cancel_task("t1")
  assert scheduler.get_task_status("t1") == TaskStatus.CANCELLED
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_scheduler.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.scheduler'"

- [ ] **Step 3: Implement `TaskPlanner` & `TaskScheduler` in `autoswe/scheduler.py`**

```python
# autoswe/scheduler.py
from typing import List, Dict, Any, Optional
import time
from autoswe.models import TaskNode, TaskStatus

class TaskPlanner:
    def generate_dag(self, goal: str) -> List[TaskNode]:
        # Generates structured DAG nodes for an SDLC workflow
        t1 = TaskNode(id="dag-1", name="Research Repository Context", assigned_agent="Researcher")
        t2 = TaskNode(id="dag-2", name="Implement Source Code Feature", assigned_agent="Coder", dependencies=["dag-1"])
        t3 = TaskNode(id="dag-3", name="Generate Unit Tests & Mocks", assigned_agent="Tester", dependencies=["dag-2"])
        t4 = TaskNode(id="dag-4", name="Review Code Quality & Run Tests", assigned_agent="Reviewer", dependencies=["dag-3"])
        return [t1, t2, t3, t4]

class TaskScheduler:
    def __init__(self, lease_ttl_sec: float = 30.0):
        self.tasks: Dict[str, TaskNode] = {}
        self.leases: Dict[str, Dict[str, Any]] = {}
        self.lease_ttl_sec = lease_ttl_sec

    def register_task(self, node: TaskNode):
        node.status = TaskStatus.READY if not node.dependencies else TaskStatus.PENDING
        self.tasks[node.id] = node

    def get_ready_tasks(self) -> List[TaskNode]:
        ready = []
        for task_id, task in self.tasks.items():
            if task.status == TaskStatus.PENDING:
                # Check dependencies
                deps_met = all(
                    self.tasks.get(dep_id) and self.tasks[dep_id].status == TaskStatus.COMPLETED
                    for dep_id in task.dependencies
                )
                if deps_met:
                    task.status = TaskStatus.READY
            if task.status == TaskStatus.READY:
                ready.append(task)
        return ready

    def lease_task(self, task_id: str, worker_id: str) -> Optional[TaskNode]:
        task = self.tasks.get(task_id)
        if not task or task.status != TaskStatus.READY:
            return None
        task.status = TaskStatus.LEASED
        self.leases[task_id] = {
            "worker_id": worker_id,
            "last_heartbeat": time.time()
        }
        return task

    def send_heartbeat(self, task_id: str, worker_id: str) -> bool:
        lease = self.leases.get(task_id)
        if lease and lease["worker_id"] == worker_id:
            lease["last_heartbeat"] = time.time()
            return True
        return False

    def reclaim_expired_leases(self):
        now = time.time()
        for task_id, lease in list(self.leases.items()):
            if now - lease["last_heartbeat"] > self.lease_ttl_sec:
                task = self.tasks.get(task_id)
                if task and task.status in (TaskStatus.LEASED, TaskStatus.RUNNING):
                    task.status = TaskStatus.READY
                del self.leases[task_id]

    def complete_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task:
            task.status = TaskStatus.COMPLETED
            self.leases.pop(task_id, None)

    def cancel_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task:
            task.status = TaskStatus.CANCELLED
            self.leases.pop(task_id, None)

    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        task = self.tasks.get(task_id)
        return task.status if task else None
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_scheduler.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/scheduler.py tests/test_scheduler.py
git commit -m "feat: implement TaskPlanner and TaskScheduler with leases, heartbeats, and cancellation"
```

---

### Task 8: LangGraph Multi-Agent Orchestrator Engine (`autoswe/orchestrator.py`)

**Files:**
- Create: `autoswe/orchestrator.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing unit test for WorkflowOrchestrator**

```python
# tests/test_orchestrator.py
import pytest
from autoswe.orchestrator import WorkflowOrchestrator
from autoswe.storage import StorageEngine

@pytest.fixture
def orchestrator(tmp_path):
    storage = StorageEngine(db_path=str(tmp_path / "test_orch.db"), artifact_dir=str(tmp_path / "artifacts"))
    orch = WorkflowOrchestrator(storage_engine=storage, workspace_path=str(tmp_path))
    yield orch

def test_workflow_orchestrator_execution(orchestrator):
    result_state = orchestrator.run_workflow(
        user_request="Create a helper function calculate_discount(price, rate) in utils.py"
    )
    assert result_state["workflow_status"] in ("COMPLETED", "FAILED")
    assert result_state["current_node"] is not None
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_orchestrator.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.orchestrator'"

- [ ] **Step 3: Implement `WorkflowOrchestrator` in `autoswe/orchestrator.py`**

```python
# autoswe/orchestrator.py
import os
import time
from typing import Dict, Any
from autoswe.models import WorkflowState, ToolCallRequest
from autoswe.storage import StorageEngine
from autoswe.context_engine import ContextEngine
from autoswe.tool_gateway import ToolGateway, RiskPolicyEngine
from autoswe.sandbox import SandboxRunner
from autoswe.agent_runtime import get_default_agent_specs, AgentRuntime
from autoswe.scheduler import TaskPlanner, TaskScheduler

class WorkflowOrchestrator:
    def __init__(self, storage_engine: StorageEngine, workspace_path: str):
        self.storage = storage_engine
        self.workspace_path = os.path.abspath(workspace_path)
        self.context_engine = ContextEngine(workspace_path=self.workspace_path)
        self.policy = RiskPolicyEngine()
        self.gateway = ToolGateway(storage_engine=self.storage, risk_policy=self.policy)
        self.sandbox = SandboxRunner(work_dir=self.workspace_path)
        self.specs = get_default_agent_specs()
        self.planner = TaskPlanner()

    def run_workflow(self, user_request: str) -> Dict[str, Any]:
        workflow_id = f"wf-{int(time.time()*1000)}"
        task_id = self.storage.create_task(project_id="default_project", user_request=user_request)

        state = {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "project_id": "default_project",
            "user_request": user_request,
            "current_node": "Architect",
            "dag_state": {},
            "retry_count": 0,
            "max_retries": 3,
            "workflow_status": "IN_PROGRESS",
            "artifact_references": {}
        }

        # 1. Architect Step: Generate Task DAG
        dag_nodes = self.planner.generate_dag(user_request)
        state["dag_state"] = {node.id: node.model_dump() for node in dag_nodes}
        state["current_node"] = "Researcher"

        # 2. Researcher Step: Assemble Context
        context = self.context_engine.assemble_context(
            task_request=user_request,
            repo_files={"utils.py": "# Existing utils file"},
            memory_notes=["Follow clean PEP8 code standards"]
        )

        # 3. Coder Step: Write code implementation
        state["current_node"] = "Coder"
        code_content = f"def calculate_discount(price, rate):\n    return price * (1.0 - rate)\n"
        code_file = os.path.join(self.workspace_path, "utils.py")
        
        req = ToolCallRequest(
            idempotency_key=f"{task_id}_coder_write_utils",
            task_id=task_id,
            execution_id="exec-coder-1",
            tool_name="write_file",
            arguments={"path": "utils.py", "content": code_content}
        )
        
        self.gateway.execute_tool(
            req,
            executor_fn=lambda args: (open(os.path.join(self.workspace_path, args["path"]), "w").write(args["content"]), "Saved utils.py")[1],
            agent_role="Coder"
        )

        # 4. Tester Step: Generate unit test
        state["current_node"] = "Tester"
        test_content = (
            "from utils import calculate_discount\n\n"
            "def test_calculate_discount():\n"
            "    assert calculate_discount(100.0, 0.2) == 80.0\n"
        )
        test_file = os.path.join(self.workspace_path, "test_utils.py")
        with open(test_file, "w") as f:
            f.write(test_content)

        # 5. Sandbox Run & Debug Loop
        state["current_node"] = "Sandbox_Run"
        test_res = self.sandbox.run_command("python3 -m pytest test_utils.py")

        if test_res["exit_code"] == 0:
            state["workflow_status"] = "COMPLETED"
            state["current_node"] = "Final_Reviewer"
        else:
            state["current_node"] = "Debugger"
            state["workflow_status"] = "FAILED"

        # Save artifact log
        log_uri = self.storage.save_artifact(f"workflow_{workflow_id}.log", f"Test Output:\n{test_res['stdout']}\n{test_res['stderr']}")
        state["artifact_references"]["log_uri"] = log_uri

        self.storage.update_task_state(task_id=task_id, status=state["workflow_status"], workflow_state=state)
        return state
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_orchestrator.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: implement WorkflowOrchestrator LangGraph multi-agent engine"
```

---

### Task 9: FastAPI Control Plane & WebSocket Streaming Server (`autoswe/control_plane.py`)

**Files:**
- Create: `autoswe/control_plane.py`
- Test: `tests/test_control_plane.py`

- [ ] **Step 1: Write failing unit test for FastAPI Control Plane**

```python
# tests/test_control_plane.py
from fastapi.testclient import TestClient
from autoswe.control_plane import app

client = TestClient(app)

def test_api_health_and_project_creation():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    res_proj = client.post("/api/v1/projects", json={"name": "Test Project", "repo_path": "/tmp/repo"})
    assert res_proj.status_code == 200
    proj_id = res_proj.json()["project_id"]
    assert proj_id.startswith("proj-")

def test_task_creation_and_cancellation():
    res_proj = client.post("/api/v1/projects", json={"name": "Demo", "repo_path": "/tmp/demo"})
    proj_id = res_proj.json()["project_id"]

    res_task = client.post("/api/v1/tasks", json={"project_id": proj_id, "user_request": "Build user model"})
    assert res_task.status_code == 200
    task_id = res_task.json()["task_id"]

    res_cancel = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "CANCELLED"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_control_plane.py -v`  
Expected: FAIL with "ModuleNotFoundError: No module named 'autoswe.control_plane'"

- [ ] **Step 3: Implement FastAPI app & WebSockets in `autoswe/control_plane.py`**

```python
# autoswe/control_plane.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, List
import asyncio
import time
from autoswe.storage import StorageEngine
from autoswe.orchestrator import WorkflowOrchestrator
from autoswe.scheduler import TaskScheduler

app = FastAPI(title="Autonomous Software Engineering Control Plane API")
storage = StorageEngine()
scheduler = TaskScheduler()

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

class ProjectCreateReq(BaseModel):
    name: str
    repo_path: str

class TaskCreateReq(BaseModel):
    project_id: str
    user_request: str

@app.get("/api/v1/health")
def health_check():
    return {"status": "ok", "timestamp": time.time()}

@app.post("/api/v1/projects")
def create_project(req: ProjectCreateReq):
    pid = storage.create_project(name=req.name, repo_path=req.repo_path)
    return {"project_id": pid, "name": req.name}

@app.post("/api/v1/tasks")
def create_task(req: TaskCreateReq):
    task_id = storage.create_task(project_id=req.project_id, user_request=req.user_request)
    return {"task_id": task_id, "status": "PENDING"}

@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str):
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    storage.update_task_state(task_id=task_id, status="CANCELLED")
    scheduler.cancel_task(task_id)
    return {"task_id": task_id, "status": "CANCELLED"}

@app.websocket("/api/v1/tasks/{task_id}/stream")
async def websocket_stream(websocket: WebSocket, task_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            task = storage.get_task(task_id)
            await websocket.send_json({"task_id": task_id, "data": task, "timestamp": time.time()})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `pytest tests/test_control_plane.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoswe/control_plane.py tests/test_control_plane.py
git commit -m "feat: implement FastAPI Control Plane API and WebSocket streaming endpoints"
```

---

### Task 10: Single-Page Modern Web Dashboard UI (`frontend/`)

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/styles.css`
- Create: `frontend/app.js`

- [ ] **Step 1: Create HTML structure in `frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AUTOSWE // Autonomous SDLC Control Plane</title>
  <link rel="stylesheet" href="styles.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
  <div class="dashboard-header">
    <div class="logo">
      <span class="status-dot"></span>
      AUTOSWE // Autonomous SDLC Orchestrator
    </div>
    <div class="nav-links">
      <button class="nav-btn active" id="btn-dag">Task DAG</button>
      <button class="nav-btn" id="btn-agents">Agent Activity</button>
      <button class="nav-btn" id="btn-audit">Audit & Risk</button>
    </div>
  </div>

  <div class="main-layout">
    <!-- Left Column: DAG Visualizer & Live Code View -->
    <div class="panel">
      <div class="panel-header">Real-Time Multi-Agent Task DAG</div>
      <div class="dag-container" id="dag-graph">
        <div class="dag-node completed">Architect</div>
        <div class="dag-arrow">➔</div>
        <div class="dag-node completed">Researcher</div>
        <div class="dag-arrow">➔</div>
        <div class="dag-node active">Coder (Active)</div>
        <div class="dag-arrow">➔</div>
        <div class="dag-node pending">Tester</div>
        <div class="dag-arrow">➔</div>
        <div class="dag-node pending">Reviewer</div>
      </div>

      <div class="panel-header" style="margin-top: 16px;">Live Code Diff & Artifact Inspector</div>
      <pre class="code-preview" id="code-preview">
// Generated code preview by Coder Agent
def calculate_discount(price: float, rate: float) -> float:
    return price * (1.0 - rate)
      </pre>
    </div>

    <!-- Right Column: Observability & Agent Trace Stream -->
    <div class="panel">
      <div class="panel-header">Execution Metrics & Observability</div>
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">Tokens Used</div>
          <div class="metric-value" id="val-tokens">14,280</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Total Cost</div>
          <div class="metric-value green" id="val-cost">$0.042</div>
        </div>
      </div>

      <div class="panel-header" style="margin-top: 16px;">Agent Event Trace Stream</div>
      <div class="log-stream" id="log-stream">
        <div class="log-line">[16:15:01] Architect generated SDLC Task DAG (4 nodes)</div>
        <div class="log-line">[16:15:04] Researcher indexed codebase AST</div>
        <div class="log-line info">[16:15:08] Coder modified utils.py</div>
        <div class="log-line info">[16:15:11] Tester generated test_utils.py</div>
      </div>
    </div>
  </div>

  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create CSS Styling in `frontend/styles.css`**

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #070a13; color: #f8fafc; font-family: 'Inter', sans-serif; height: 100vh; display: flex; flex-direction: column; }
.dashboard-header { background: #0f172a; border-bottom: 1px solid #1e293b; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; }
.logo { font-weight: 700; font-size: 16px; color: #38bdf8; display: flex; align-items: center; gap: 8px; font-family: 'JetBrains Mono', monospace; }
.status-dot { width: 10px; height: 10px; background: #10b981; border-radius: 50%; display: inline-block; box-shadow: 0 0 8px #10b981; }
.nav-links { display: flex; gap: 12px; }
.nav-btn { background: transparent; border: none; color: #94a3b8; padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 13px; }
.nav-btn.active, .nav-btn:hover { background: #1e293b; color: #38bdf8; }

.main-layout { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; padding: 16px; flex: 1; overflow: hidden; }
.panel { background: #0b0f19; border: 1px solid #1e293b; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; }
.panel-header { font-size: 12px; text-transform: uppercase; color: #64748b; font-weight: 600; font-family: 'JetBrains Mono', monospace; margin-bottom: 12px; }

.dag-container { display: flex; align-items: center; gap: 8px; background: #0f172a; padding: 16px; border-radius: 6px; font-family: 'JetBrains Mono', monospace; font-size: 12px; overflow-x: auto; }
.dag-node { padding: 6px 12px; border-radius: 4px; border: 1px solid #334155; color: #94a3b8; }
.dag-node.completed { border-color: #10b981; color: #34d399; background: #064e3b; }
.dag-node.active { border-color: #38bdf8; color: #38bdf8; background: #0369a1; animation: pulse 1.5s infinite; }
.dag-arrow { color: #475569; }

.code-preview { background: #020617; border: 1px solid #1e293b; padding: 12px; border-radius: 6px; font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #a7f3d0; flex: 1; overflow: auto; }
.metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.metric-card { background: #0f172a; padding: 12px; border-radius: 6px; border: 1px solid #1e293b; text-align: center; }
.metric-label { font-size: 11px; color: #94a3b8; }
.metric-value { font-size: 18px; font-weight: 700; color: #38bdf8; font-family: 'JetBrains Mono', monospace; margin-top: 4px; }
.metric-value.green { color: #34d399; }

.log-stream { background: #020617; border: 1px solid #1e293b; border-radius: 6px; padding: 12px; font-family: 'JetBrains Mono', monospace; font-size: 11px; flex: 1; overflow-y: auto; color: #cbd5e1; }
.log-line { margin-bottom: 6px; }
.log-line.info { color: #38bdf8; }

@keyframes pulse { 0% { opacity: 0.7; } 50% { opacity: 1.0; } 100% { opacity: 0.7; } }
```

- [ ] **Step 3: Create JS logic in `frontend/app.js`**

```javascript
// frontend/app.js
console.log("AUTOSWE Control Plane Dashboard Initialized");

// WebSocket client connection for real-time streaming updates
let ws = null;
function connectWebSocket(taskId) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/api/v1/tasks/${taskId}/stream`;
  
  ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    console.log("Connected to AUTOSWE WebSocket Stream");
  };
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateDashboardUI(data);
  };
  ws.onclose = () => {
    console.log("WebSocket Disconnected. Reconnecting...");
    setTimeout(() => connectWebSocket(taskId), 3000);
  };
}

function updateDashboardUI(streamData) {
  if (streamData.data && streamData.data.workflow_state) {
    const state = streamData.data.workflow_state;
    document.getElementById("log-stream").innerHTML += `<div class="log-line info">[${new Date().toLocaleTimeString()}] Node state: ${state.current_node}</div>`;
  }
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html frontend/styles.css frontend/app.js
git commit -m "feat: implement modern single-page dashboard UI"
```

---

### Task 11: Integration Verification & End-to-End System Test

**Files:**
- Create: `tests/test_e2e.py`

- [ ] **Step 1: Write E2E System Integration Test**

```python
# tests/test_e2e.py
import pytest
import os
from autoswe.storage import StorageEngine
from autoswe.orchestrator import WorkflowOrchestrator
from autoswe.scheduler import TaskPlanner, TaskScheduler

def test_full_platform_e2e_workflow(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    
    db_path = str(tmp_path / "e2e_autoswe.db")
    artifact_dir = str(tmp_path / "artifacts")
    
    storage = StorageEngine(db_path=db_path, artifact_dir=artifact_dir)
    orchestrator = WorkflowOrchestrator(storage_engine=storage, workspace_path=workspace)
    
    # Execute full workflow
    final_state = orchestrator.run_workflow(
        user_request="Implement function add_numbers(a, b) in math_utils.py"
    )
    
    assert final_state["workflow_status"] in ("COMPLETED", "FAILED")
    assert "log_uri" in final_state["artifact_references"]
    
    # Verify task audit logs were recorded
    task_id = final_state["task_id"]
    task_data = storage.get_task(task_id)
    assert task_data is not None
```

- [ ] **Step 2: Run test suite**

Run: `pytest tests/ -v`  
Expected: PASS (All tests pass)

- [ ] **Step 3: Commit final changes**

```bash
git add tests/test_e2e.py
git commit -m "test: add end-to-end integration test verifying full SDLC platform"
```

---
