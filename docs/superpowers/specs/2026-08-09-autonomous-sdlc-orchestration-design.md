# Autonomous Software Engineering Platform — Multi-Agent SDLC Orchestration Design Document

**Date:** 2026-08-09  
**Status:** Production-Ready (V2 - Architecturally Enhanced)  
**Author:** Antigravity AI Assistant & Engineering Team  

---

## 1. Executive Summary & Objectives

The **Autonomous Software Engineering Platform** is a production-grade multi-agent software engineering system built with **LangGraph**, **LangChain**, and **LangSmith**. It orchestrates specialized AI agents (**Architect**, **Coder**, **Test Generator**, **Reviewer**, **Researcher**, **Debugger**, and **Final Reviewer**) across complex, multi-task software development lifecycles (SDLC).

### Key Architectural Pillars:
1. **Decoupled Workflow State & External Artifact Persistence**: Light `WorkflowState` containing workflow metadata and artifact handles. Large payloads (patches, logs, generated code, traces) are persisted in dedicated storage systems (PostgreSQL, Object/File Storage, Git working trees, Vector DB).
2. **First-Class Task DAG & Scheduler Engine**: Clear separation between high-level **Task Planning** (dependency graph generation), **Task Scheduling** (stateful lifecycle management: `PENDING`, `READY`, `RUNNING`, `BLOCKED`, `COMPLETED`), and **LangGraph Worker Execution**.
3. **Dynamic Risk Engine for Tool Execution**: Evaluates tool calls using a risk engine (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`), automatically executing safe commands (pytest, git diff) while enforcing interactive human approval gates for high-risk actions (git push, deployment).
4. **Declarative Agent Runtime & `AgentSpec`**: Standardized agent execution layer decoupling agent configuration (`role`, `model_policy`, `tools`, `permissions`, `context_policy`, `budget`) from orchestration plumbing.
5. **Context Engineering Subsystem**: Dedicated `ContextEngine` & `ContextBuilder` managing repository AST, memory history, symbol resolution, and context window pruning to handle large codebases without context stuffing.
6. **LLM Observability & Evaluation**: Integrated LangSmith end-to-end tracing, prompt/version tracking, real-time token/latency/cost metrics, and regression benchmarks.
7. **Control Plane & Web Dashboard**: FastAPI control plane backend paired with a modern single-page dashboard for real-time DAG state visualization, diff inspection, and live execution tracing.

---

## 2. Architecture & Subsystem Boundaries

```
                         ┌─────────────────────────────┐
                         │    Web Dashboard UI (SPA)   │
                         └──────────────┬──────────────┘
                                        │ (REST / WebSockets)
                         ┌──────────────▼──────────────┐
                         │    FastAPI Control Plane    │
                         └──────┬───────────────┬──────┘
                                │               │
                    ┌───────────▼──┐        ┌───▼───────────┐
                    │ Postgres DB  │        │ Redis Queue   │
                    │ (Metadata)   │        │ (Events/State)│
                    └──────────────┘        └───┬───────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │ Task DAG & Scheduler  │
                                    └───────────┬───────────┘
                                                │ (Dispatches Ready Tasks)
                                    ┌───────────▼───────────┐
                                    │ LangGraph Worker Exec │
                                    └───────────┬───────────┘
                                                │
            ┌───────────────────────────────────┼───────────────────────────────────┐
            ▼                                   ▼                                   ▼
┌───────────────────────┐           ┌───────────────────────┐           ┌───────────────────────┐
│   Context Engine      │           │    Agent Runtime      │           │     Model Gateway     │
│ (Repo, Memory, Task)  │           │  (Declarative Specs)  │           │ (Multi-LLM & Budget)  │
└───────────┬───────────┘           └───────────┬───────────┘           └───────────────────────┘
            │                                   │
            └─────────────────┬─────────────────┘
                              ▼
                   ┌────────────────────┐
                   │ Dynamic Risk Engine│
                   └──────────┬─────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
     Git Repository     Docker Sandbox       Artifact Store
     (Working Tree)     (Test Execution)     (Patches & Logs)
```

---

## 3. Core Subsystems Specification

### 3.1 Decoupled State & Artifact Storage Model
- **`WorkflowState` Schema**:
  ```python
  from typing import TypedDict, List, Dict, Any, Optional

  class WorkflowState(TypedDict):
      workflow_id: str
      task_id: str
      project_id: str
      user_request: str
      current_node: str
      dag_state: Dict[str, Any]  # Node statuses: PENDING, READY, RUNNING, BLOCKED, COMPLETED
      retry_budget_state: Dict[str, Any]  # retry_count, max_retries, budget_consumed_usd, budget_cap_usd
      approval_state: Dict[str, Any]  # pending_gate_id, status
      artifact_references: Dict[str, str]  # Handle URIs to external stores: patch_uri, log_uri, test_result_uri
  ```
- **Storage Mapping**:
  - **PostgreSQL**: Task records, project metadata, workflow status, audit trails.
  - **Object/File Storage (`/artifacts/`)**: Git patch files, build logs, complete test output dumps.
  - **Git Working Tree**: Target codebase source code, committed branches.
  - **Vector DB / AST Index**: Workspace code embeddings, symbol graph definitions.
  - **Redis**: Ephemeral workflow state checkpoints, pub/sub WebSocket channels, event queue.

---

### 3.2 First-Class Task DAG & Scheduler Engine

The task workflow is split into two explicit services:

1. **Task Planner (`autoswe.planner`)**:
   - Analyzes high-level goals and generates a dependency DAG (`TaskDAG`).
   - Supports dynamic parallel branching (e.g. Backend Task branch and Frontend Task branch executing concurrently).

2. **Task Scheduler (`autoswe.scheduler`)**:
   - Manages task node state machines (`PENDING` ➔ `READY` ➔ `RUNNING` ➔ `BLOCKED` ➔ `COMPLETED` / `FAILED`).
   - Monitors node dependencies: when parent tasks complete, dependent tasks automatically shift from `PENDING` to `READY` and dispatch to available LangGraph Workers.

---

### 3.3 Dynamic Risk Policy Engine for Tool Gateway

Tool executions are intercepted by `RiskPolicyEngine` before reaching the execution environment:

```
  Tool Call Request
        │
        ▼
[Risk Policy Engine]
        │
        ├── LOW      (e.g., pytest, git diff, npm test, flake8) ──────► Auto-Execute in Sandbox
        ├── MEDIUM   (e.g., pip install, curl external API)      ──────► Restricted Sandbox Sandbox Exec
        ├── HIGH     (e.g., git push to remote, branch merge)    ──────► Requires Human UI Approval Gate
        └── CRITICAL (e.g., terraform apply, production deploy)  ──────► Requires Admin Dual Approval Gate
```

---

### 3.4 Declarative Agent Runtime & `AgentSpec`

Agents are defined declaratively via `AgentSpec`:

```python
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

@dataclass
class AgentSpec:
    role: str                       # e.g., "Coder", "Architect", "Reviewer"
    description: str
    model_policy: Dict[str, Any]     # primary_model, fallback_model, temperature
    tools: List[str]                # allowed tool names
    permissions: List[str]          # permission boundaries
    context_policy: Dict[str, Any]   # max_token_limit, include_memory, symbol_depth
    budget: Dict[str, Any]          # max_cost_usd, max_execution_time_sec
    termination_policy: Dict[str, Any]
```

The **`AgentRuntime`** manages:
- Context assembly via `ContextEngine`.
- Agent execution loop with token budget enforcement.
- Tool authorization checking via `RiskPolicyEngine`.
- Trace propagation to LangSmith.

---

### 3.5 Dedicated Context Engineering Layer

The **`ContextEngine`** controls what enters the agent's LLM context window:

```
                    ContextEngine
                          │
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
 Repository Context    Memory Context    Task Context
 (AST, Symbols, Files) (Past Decisions)  (Goal, Constraints)
       │                  │                  │
       └──────────────────┼──────────────────┘
                          ▼
                   ContextBuilder
                          │
                          ├── Token Window Allocation
                          ├── Relevance Scoring & Pruning
                          └── AST Symbol Resolution
                          │
                          ▼
                Optimized Model Prompt
```

---

## 4. End-to-End Multi-Agent SDLC Workflow Flow

```
User Goal / Issue
       │
       ▼
 [Architect] ──► Generates Task DAG
       │
       ▼
 [Researcher] ──► ContextEngine Indexes Repo & AST
       │
       ▼
 [Task Scheduler] ──► Dispatches Ready Tasks to LangGraph Workers
       │
       ├───────────────────────────────────────────────┐
       ▼                                               ▼
[Backend Task Branch]                          [Frontend Task Branch]
  ├── Coder Agent                                ├── Coder Agent
  ├── Test Generator Agent                       ├── Test Generator Agent
  └── Reviewer Agent                             └── Reviewer Agent
       │                                               │
       └───────────────────────┬───────────────────────┘
                               ▼
                   [Integration Node / Sandbox Execution]
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
          [Tests Pass?]                 [Tests Fail?]
                │                             │
                │                             ▼
                │                      [Debugger Agent]
                │                             │
                │                             ▼ (Loop to Coder if Retries < Max)
                │                      Self-Healing Feedback
                │
                ▼
        [Final Reviewer Agent]
                │
                ▼
       [PR / Git Branch Merge]
```

---

## 5. Control Plane API & Web Dashboard Specification

### 5.1 FastAPI Control Plane (`autoswe.control_plane`)
- `POST /api/v1/projects`: Register codebase repositories.
- `POST /api/v1/tasks`: Submit user software engineering requests.
- `GET /api/v1/tasks/{id}`: Fetch task state, DAG graph status, and artifact handles.
- `WS /api/v1/tasks/{id}/stream`: WebSocket endpoint for streaming live DAG graph state changes, agent logs, and cost/token metrics.
- `POST /api/v1/approval/{id}`: Approve or reject pending `HIGH`/`CRITICAL` risk tool execution gates.

### 5.2 Single-Page Web Dashboard UI (`frontend/`)
- **Dynamic Task-DAG View**: Canvas/SVG interactive graph showing task node statuses, execution branches, and active self-healing retries.
- **Git Patch & Code Inspector**: Visual split diff preview for code generated by Coder & Debugger.
- **Risk Policy & Approval Gate Modal**: Popup UI for approving sensitive commands with detailed risk score explanations.
- **Observability Panel**: Live token counter, execution latency charts, cost estimator ($ USD), and LangSmith trace deep-links.

---

## 6. Verification Plan & Quality Gates

1. **Automated Unit Testing**:
   - Verify `TaskScheduler` DAG node dependency transitions.
   - Verify `RiskPolicyEngine` risk scoring (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
   - Verify `ContextBuilder` token pruning and symbol resolution.
   - Verify `AgentRuntime` execution with budget limit enforcement.

2. **Integration & Sandbox Verification**:
   - Execute an end-to-end multi-agent feature build task with unit test generation and self-healing error recovery.
   - Verify artifact storage offloading (patches and test logs saved to `/artifacts/`).
   - Confirm WebSocket event delivery to the frontend dashboard.

---
