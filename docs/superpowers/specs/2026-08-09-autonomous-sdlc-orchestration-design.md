# Autonomous Software Engineering Platform — Multi-Agent SDLC Orchestration Design Document

**Date:** 2026-08-09  
**Status:** Approved  
**Author:** Antigravity AI Assistant & Engineering Team  

---

## 1. Executive Summary & Objectives

The **Autonomous Software Engineering Platform** is a production-grade multi-agent software-engineering platform built with **LangGraph**, **LangChain**, and **LangSmith**. It orchestrates 6 specialized AI agents (**Architect**, **Coder**, **Reviewer**, **Tester**, **Researcher**, and **Debugger**) through stateful, fault-tolerant software development lifecycle (SDLC) workflows.

### Key Capabilities:
- **Dynamic Task-DAG Execution Engine**: Decomposes high-level natural language requirements or bug descriptions into dependency-aware execution DAGs supporting parallel agent execution, persistent workflow checkpoints, RAG-based codebase intelligence, automated test generation, and code review.
- **Adaptive Self-Healing Debug Loops**: Automatically captures pytest/jest stack traces on sandbox test failures, performs root-cause analysis, and loops back to Coder agents for iterative self-healing with strict budget caps and safety circuit breakers.
- **Secure Tool Execution Gateway & Sandbox**: Enforces sandboxed command execution (Docker / sandboxed subprocess), RBAC policies, secret isolation/redaction, approval gates for destructive commands, and immutable audit logging.
- **LLM Observability & Evaluation**: Integrates end-to-end LangSmith tracing, prompt/version tracking, real-time token/latency/cost metrics, and automated software engineering benchmarks.
- **Control Plane & Web Dashboard**: Provides a FastAPI backend with WebSocket streaming and an interactive single-page web dashboard displaying real-time task DAG visualization, live execution metrics, git diff viewers, and approval gate controls.

---

## 2. Architecture & Subsystem Boundaries

The platform follows a **Unified Microservices Monorepo Architecture** structured as follows:

```
                          ┌───────────────────────────┐
                          │   Web UI Dashboard (SPA)  │
                          └─────────────┬─────────────┘
                                        │ (REST / WebSocket)
                          ┌─────────────▼─────────────┐
                          │   FastAPI Control Plane   │
                          └──────┬─────────────┬──────┘
                                 │             │
                    ┌────────────▼──┐       ┌──▼────────────┐
                    │ Postgres/SQLite│       │ Redis Event Q │
                    └───────────────┘       └──┬────────────┘
                                               │
                                    ┌──────────▼───────────┐
                                    │   LangGraph Engine   │
                                    └──────────┬───────────┘
                                               │
                       ┌───────────────────────┴───────────────────────┐
                       ▼                                               ▼
              ┌─────────────────┐                             ┌─────────────────┐
              │  Agent Runtime  │                             │  Model Gateway  │
              │ 6 Agent Roles   │                             │  Multi-LLM      │
              └────────┬────────┘                             └─────────────────┘
                       │
              ┌────────▼────────┐
              │  Tool Gateway   │ (RBAC, Sandbox, Secret Masking, Audit)
              └────────┬────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
     Git Repos      Docker Sandbox   RAG Store
```

### Module Blueprint (`autoswe/` Python Package)
- `autoswe.control_plane`: FastAPI REST controllers, WebSocket manager, project & task state endpoints.
- `autoswe.engine`: LangGraph `StateGraph` workflows, conditional edge routers, state models, and checkpoint persistence.
- `autoswe.agents`: Implementations for Architect, Coder, Reviewer, Tester, Researcher, and Debugger agents.
- `autoswe.gateways.model`: Multi-LLM provider router (Gemini, OpenAI, Anthropic, Ollama), cost budgeting, and fallback logic.
- `autoswe.gateways.tool`: Tool permission checking, secret redaction, interactive approval gate trigger, and audit logger.
- `autoswe.sandboxing`: Docker sandbox container manager and subprocess isolation runner.
- `autoswe.rag`: Workspace code parser, AST indexer, and semantic RAG retriever.
- `autoswe.observability`: LangSmith tracing setup, token/cost tracker, and evaluation benchmark runner.
- `frontend/`: Single-page modern dashboard UI (HTML5, Vanilla CSS, JS/Vite).

---

## 3. Agent Roles & Stateful Workflow DAG

### 3.1 Specialized Agent Roles
1. **Architect Agent**: Receives user feature requests or bug reports, performs initial scope analysis, and generates a structured Task DAG with explicit task dependencies and completion criteria.
2. **Researcher Agent**: Uses RAG and semantic code search to inspect repository files, module imports, existing test patterns, and external documentation.
3. **Coder Agent**: Receives task instructions and research context, generates precise file edits and new source code files.
4. **Tester Agent**: Generates comprehensive unit tests and mock objects matching Coder edits.
5. **Reviewer Agent**: Performs automated static analysis, security vulnerability scanning, and adherence to project coding guidelines.
6. **Debugger Agent**: Parses test execution tracebacks, identifies failing lines of code, formulates fix strategies, and updates state context for Coder retry loops.

### 3.2 LangGraph Workflow State Schema (`AgentWorkflowState`)
```python
from typing import TypedDict, List, Dict, Any, Optional

class TaskNode(TypedDict):
    id: str
    name: str
    assigned_agent: str
    dependencies: List[str]
    status: str  # PENDING, IN_PROGRESS, COMPLETED, FAILED

class AgentWorkflowState(TypedDict):
    task_id: str
    project_path: str
    user_request: str
    plan_dag: List[TaskNode]
    research_context: Dict[str, Any]
    generated_code: Dict[str, str]  # filepath -> content
    generated_tests: Dict[str, str]  # filepath -> content
    review_feedback: Dict[str, Any]
    test_results: Dict[str, Any]  # exit_code, stdout, stderr, stack_traces
    retry_count: int
    max_retries: int
    budget_consumed: float  # cumulative USD
    budget_limit: float
    workflow_status: str  # IN_PROGRESS, AWAITING_APPROVAL, COMPLETED, FAILED
```

### 3.3 Dynamic Graph Execution & Self-Healing Loop
```
[START] ➔ Architect ➔ Researcher ➔ Parallel(Coder, Tester) ➔ Reviewer ➔ Sandbox Run
                                                                               │
                                                   ┌───────────────────────────┴───────────────────────────┐
                                                   ▼                                                       ▼
                                             [Tests Pass]                                             [Tests Fail]
                                                   │                                                       │
                                                   ▼                                                       ▼
                                               [COMMIT] ➔ [END]                                     Debugger Agent
                                                                                                           │
                                                                                                           ▼
                                                                                                (Retries < Max & Budget OK?)
                                                                                                   ├── Yes ➔ Loop to Coder
                                                                                                   └── No  ➔ Circuit Breaker Fail
```

---

## 4. Security, Tool Execution Gateway & Sandboxing

### 4.1 Tool Gateway Security Layer
- **Permission Matrix**: Tools are tagged with risk categories (`READ_ONLY`, `WORKSPACE_EDIT`, `SYSTEM_EXEC`).
- **Approval Gate**: Any `SYSTEM_EXEC` command (e.g., shell execution, git push) automatically pauses workflow state and posts an approval prompt to the Control Plane API/UI.
- **Secret Redactor**: Applies regex masks (API keys, OAuth tokens, passwords) on all stdout/stderr logs and model prompts.
- **Audit Logger**: Appends every tool call to `audit_logs` (capturing timestamp, agent ID, tool name, sanitized inputs, exit code, and execution time).

### 4.2 Sandboxed Code Execution
- **Docker Container Runner**: Runs code and test suites inside isolated Docker containers with non-root user execution, memory limit (512MB), CPU limit (1.0 core), and strict execution timeout (default 30 seconds).
- **Process Isolation Fallback**: Fallback sandbox runner for environments without Docker using isolated subprocesses, sanitized `ENV` maps, and process group lifecycle limits.

---

## 5. LLM Observability & Evaluation Infrastructure

### 5.1 LangSmith Tracing & Metadata
- End-to-end tracing enabled via LangSmith callbacks.
- All LLM calls and tool executions are tagged with `task_id`, `agent_role`, `model_name`, `prompt_version`, and `project_name`.

### 5.2 Metrics & Budget Tracking
- Real-time token consumption tracking (Prompt Tokens, Completion Tokens).
- Cumulative cost calculation based on LLM pricing tables.
- Latency metrics (Time to First Token, Step Duration).
- Task success rates and self-healing iteration efficiency.

### 5.3 Automated Evaluation Suite (`autoswe.eval`)
- Evaluation metrics:
  1. **Test Pass Rate**: Percentage of pytest unit tests passing cleanly.
  2. **Code Hygiene Index**: Static analysis clean score (linter warnings/errors).
  3. **Self-Healing Loop Efficiency**: Number of retry loops required to reach passing status.
  4. **Regression Benchmark Runner**: Re-executes historical prompt suites against test benchmarks.

---

## 6. Control Plane API & Modern Dashboard UI

### 6.1 Control Plane REST & WebSocket API
- `POST /api/v1/projects`: Create/register repository workspace.
- `POST /api/v1/tasks`: Submit software engineering requests.
- `GET /api/v1/tasks/{id}`: Get full task status, DAG state, and generated diffs.
- `WS /api/v1/tasks/{id}/stream`: WebSocket channel for streaming real-time graph updates, agent activity logs, and token/cost metrics.
- `POST /api/v1/approval/{id}`: Resolve pending approval gates (approve/reject).

### 6.2 Modern Dashboard UI (`frontend/`)
- Built using HTML5, Vanilla CSS, and modern JavaScript.
- Features:
  - **Live Task DAG Visualizer**: Interactive workflow graph highlighting node execution states in real-time over WebSocket.
  - **Git Code Diff Viewer**: Split-screen diff viewer for inspectable code changes.
  - **Observability Metrics Dashboard**: Live counters for tokens, cost ($ USD), latency, and trace links.
  - **Approval Gate Modal**: Interactive approval dialog for high-risk tool execution.

---

## 7. Verification & Acceptance Criteria

1. **Unit & Component Testing**:
   - Test LangGraph workflow state transitions and edge routers.
   - Test Tool Gateway permission checks, secret redaction, and audit logging.
   - Test Sandboxed Execution timeout enforcement and container/subprocess isolation.
   - Test Control Plane REST endpoints and WebSocket message streaming.

2. **Integration Verification**:
   - Execute an end-to-end automated software engineering task (e.g. implementing a feature with unit tests and self-healing bug fix loop).
   - Validate real-time WebSocket state delivery to the web dashboard.
   - Confirm audit logs and LangSmith tracing metadata are recorded cleanly.

---
