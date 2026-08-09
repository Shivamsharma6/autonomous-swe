# Autonomous Software Engineering Platform — Multi-Agent SDLC Orchestration Design Document

**Date:** 2026-08-09  
**Status:** Approved & Production-Ready (V3 - Final Architecture)  
**Author:** Antigravity AI Assistant & Engineering Team  

---

## 1. Executive Summary & Core Objectives

The **Autonomous Software Engineering Platform** is a production-grade control plane for multi-agent software engineering workflows. Built on **LangGraph**, **FastAPI**, **PostgreSQL**, **Redis**, and **LangSmith**, it orchestrates 7 specialized AI agents (**Architect**, **Researcher**, **Coder**, **Test Generator**, **Reviewer**, **Debugger**, and **Final Reviewer**) across complex, multi-task software development lifecycles.

### Key Architectural Pillars:
1. **Durable Control Plane & Clean Persistence Layers**:
   - **PostgreSQL**: Absolute system of record (task state, worker leases, RBAC, audit logs).
   - **Redis / Redis Streams**: Ephemeral caching, distributed locks, pub/sub WebSocket event streaming.
   - **Object / File Storage**: Artifacts (git patches, test execution stdout/stderr logs, build traces).
   - **Vector / AST Database**: Codebase structural embeddings and symbol dependency graphs.
2. **First-Class Task Scheduler with Worker Leases & Heartbeats**:
   - Manages dependency resolution, concurrency bounds, task priorities, worker lease renewal via periodic heartbeats, cancellation propagation, and dead-letter queue (DLQ) handling.
3. **Idempotent Tool Execution & Dynamic Risk Engine**:
   - Intercepts tool calls via `RiskPolicyEngine` (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
   - Ensures exactly-once execution semantics using `idempotency_key` verification for side-effecting operations (e.g. `git push`).
4. **Declarative `AgentSpec` & Standardized `AgentRuntime`**:
   - Decouples agent capabilities from orchestration logic via structured declarations (`role`, `model_policy`, `tools`, `permissions`, `context_policy`, `budget`).
5. **4-Dimensional Context Engineering Subsystem**:
   - `ContextEngine` constructs prompts across 4 dimensions: **Repository Context**, **Task Context**, **Memory Context**, and **Execution Context** (diffs, stack traces, previous attempts).
6. **Task Cancellation & Worker Lifecycle Management**:
   - Real-time cancellation propagation from API (`POST /api/v1/tasks/{id}/cancel`) down through Scheduler, LangGraph Workers, and running Sandbox subprocesses.
7. **Controlled PR Policy Gate**:
   - Final Reviewer submits changes through a policy gate. Low-risk changes create PRs automatically; high-risk changes require interactive human approval in the UI. (No automated auto-merges by default).

---

## 2. Target High-Level Architecture (HLD)

```
                        ┌─────────────────┐
                        │   Web / CLI / API│
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │  Control Plane  │ (Auth / RBAC / Cancel API)
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │ Task / Workflow │
                        │    Manager      │
                        └────────┬────────┘
                                 ▼
                    ┌────────────────────────┐
                    │   Planner + Task DAG   │
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │ Scheduler / Dispatcher │ (Leases, Heartbeats, DLQ,
                    │                        │  Retries, Idempotency)
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │    LangGraph Worker    │
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │     Agent Runtime      │ (AgentSpec / Lifecycle)
                    └──────┬───────┬─────────┘
                           │       │
                 ┌─────────┘       └─────────┐
                 ▼                           ▼
          ┌─────────────┐             ┌─────────────┐
          │ Context     │             │ Model       │
          │ Engine      │             │ Gateway     │
          └─────────────┘             └─────────────┘
                           │
                           ▼
                    ┌────────────────┐
                    │  Tool Gateway  │ (Idempotency Key Check)
                    └───────┬────────┘
                            ▼
                    ┌────────────────┐
                    │  Risk Engine   │ (LOW ➔ Auto, MED ➔ Sandbox, HIGH/CRIT ➔ Gate)
                    └───────┬────────┘
                            ▼
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
       GitHub             Sandbox          Artifact Store
          │                 │                  │
          └─────────────────┼──────────────────┘
                            ▼
                     CI / Test / Scan
                            │
                            ▼
                      Review / Policy ──► Approval Gate ──► Create PR
```

---

## 3. Deep-Dive Design: The 4 Core Subsystems

### 3.1 Subsystem 1: Task DAG & Scheduler Semantics

The **`TaskScheduler`** manages the life cycle of individual tasks in a workflow DAG.

#### State Machine:
`PENDING` ➔ `READY` ➔ `LEASED (RUNNING)` ➔ `COMPLETED` / `FAILED` / `RECOVERABLE` / `CANCELLED`

#### Scheduler Capabilities:
- **Dependency Resolution**: Evaluates DAG parent node completion before transitioning child nodes from `PENDING` to `READY`.
- **Worker Leases & Heartbeats**:
  - When a LangGraph Worker picks up a task, it acquires a lease (e.g. `ttl=30s`).
  - Worker sends heartbeats every 5 seconds.
  - If a worker crashes and lease expires without heartbeat, task shifts to `RECOVERABLE` ➔ `READY` and is re-leased to another worker.
- **Concurrency & Priority Controls**: Enforces max parallel workers per project and executes highest priority task nodes first.
- **Dead-Letter Queue (DLQ)**: If a task exceeds `max_retries` (default: 3), it is moved to DLQ with detailed failure metadata for human inspection.
- **Cancellation Propagation**: When a user cancels a task, Scheduler broadcasts a cancellation token to active worker threads and sends `SIGTERM`/`SIGKILL` to sandboxed container processes.

---

### 3.2 Subsystem 2: Agent Runtime & Context Engineering

#### Declarative `AgentSpec`
```python
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

@dataclass
class AgentSpec:
    role: str                       # Architect, Researcher, Coder, Test Generator, Reviewer, Debugger, Final Reviewer
    description: str
    model_policy: Dict[str, Any]     # primary_model, fallback_model, temperature
    tools: List[str]                # allowed tool names
    permissions: List[str]          # permission limits
    context_policy: Dict[str, Any]   # token_budget, max_symbol_depth, include_memory
    budget: Dict[str, Any]          # max_cost_usd, max_time_sec
    termination_policy: Dict[str, Any]
```

#### 4-Dimensional `ContextEngine`
```
                    ContextEngine
                          │
    ┌─────────────────────┼─────────────────────┬─────────────────────┐
    ▼                     ▼                     ▼                     ▼
Repository Context    Task Context         Memory Context     Execution Context
(AST, Symbols, Files) (Goal, Criteria)     (Past Decisions)   (Diffs, Stack Traces,
                                                               Failed Commands)
    │                     │                     │                     │
    └─────────────────────┼─────────────────────┴─────────────────────┘
                          ▼
                    ContextBuilder
                          │
                          ├── Token Window Budget Allocation
                          ├── Relevance Pruning & Compression
                          └── Symbol Graph Resolution
                          │
                          ▼
               Optimized Agent Prompt
```

- **Execution Context**: Crucial for the Debugger agent. Injects exact current git diff, failing command line, pytest/jest stack trace, environment configuration, and previous fix attempts.

---

### 3.3 Subsystem 3: Tool Execution Gateway, Risk Engine & Idempotency

#### Idempotency & Exactly-Once Semantics
Every tool invocation payload includes:
```json
{
  "idempotency_key": "task-102_attempt-1_git_commit_a8f9c",
  "task_id": "task-102",
  "execution_id": "exec-4912",
  "attempt": 1,
  "tool_name": "git_commit",
  "arguments": { "message": "feat: add user authentication endpoint" }
}
```
- Tools declare `idempotent: bool`.
- `ToolGateway` checks PostgreSQL/Redis for `idempotency_key` before execution. If an execution with the same key completed previously, the cached result is returned without re-executing side effects.

#### Risk Engine Policy
```
  Tool Call Request
        │
        ▼
 [Risk Policy Engine]
        │
        ├── LOW      (e.g., pytest, git diff, npm test, flake8) ──────► Auto-Execute in Sandbox
        ├── MEDIUM   (e.g., pip install, curl external API)      ──────► Restricted Sandbox Exec
        ├── HIGH     (e.g., git push to remote, branch merge)    ──────► Requires Human UI Approval Gate
        └── CRITICAL (e.g., terraform apply, production deploy)  ──────► Requires Admin Dual Approval Gate
```

---

### 3.4 Subsystem 4: Durability, Fault Recovery & Storage Separation

#### Strict Storage Responsibilities:
- **PostgreSQL**: System of record. Holds `projects`, `tasks`, `workflow_runs`, `worker_leases`, `audit_logs`, and `idempotency_records`.
- **Redis / Redis Streams**: Ephemeral state coordination, worker pub/sub events, WebSocket channels, and cache.
- **Object / File Storage (`/artifacts/`)**: Storage for git patch files (`.patch`), raw execution log dumps (`.log`), and coverage reports (`.json`).
- **Vector DB / AST Store**: Codebase embeddings and symbol trees.

#### Task Cancellation Flow:
```
User clicks Cancel in UI / CLI
       │
       ▼
POST /api/v1/tasks/{id}/cancel
       │
       ▼
Control Plane updates PostgreSQL state -> CANCELLED
       │
       ▼
Scheduler emits cancellation event over Redis
       │
       ▼
LangGraph Worker receives cancellation token
       │
       ▼
AgentRuntime terminates LLM generation
       │
       ▼
ToolGateway issues SIGKILL to Sandboxed Subprocess/Docker Container
```

---

## 4. Fixed Set of 7 Agents & SDLC Workflow

### 4.1 Agent Roster
1. **Architect Agent**: Requirement analysis & Task DAG generation.
2. **Researcher Agent**: Codebase indexing, AST parsing, RAG retrieval.
3. **Coder Agent**: Writes code implementations and edits files.
4. **Test Generator Agent**: Writes pytest/jest unit tests matching Coder changes.
5. **Reviewer Agent**: Code quality, security, and lint inspection.
6. **Debugger Agent**: Parses stack traces & formulates targeted fixes during self-healing loops.
7. **Final Reviewer Agent**: Evaluates completed feature against risk policy and prepares Pull Requests (PR).

### 4.2 End-to-End SDLC Flow
```
User Goal / Issue
       │
       ▼
 [Architect] ──► Task DAG Generation
       │
       ▼
 [Researcher] ──► ContextEngine Indexes AST & Symbols
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
                   [Integration Node / Sandbox Run]
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
          [Tests Pass?]                 [Tests Fail?]
                │                             │
                │                             ▼
                │                      [Debugger Agent]
                │                             │
                │                             ▼ (Loop to Coder if Retries < Max)
                │                      Self-Healing Feedback Loop
                │
                ▼
        [Final Reviewer Agent]
                │
                ▼
      [Policy / Risk Engine]
                │
       ┌────────┴────────┐
       ▼                 ▼
    [LOW Risk]      [HIGH Risk]
       │                 │
    Create PR       Approval Gate in UI
```

---

## 5. Control Plane API Specification

- `POST /api/v1/projects`: Register repository project.
- `POST /api/v1/tasks`: Submit software engineering workflow request.
- `GET /api/v1/tasks/{id}`: Query task status, DAG state, and artifact handles.
- `POST /api/v1/tasks/{id}/cancel`: Cancel running task workflow and terminate active sandbox processes.
- `WS /api/v1/tasks/{id}/stream`: WebSocket channel for live graph state changes, agent logs, and token/cost metrics.
- `POST /api/v1/approval/{id}`: Approve or reject pending `HIGH`/`CRITICAL` risk tool execution gates.

---

## 6. Verification & Quality Assurance Plan

1. **Unit & Scheduler Tests**:
   - Test `TaskScheduler` worker lease timeout and heartbeat recovery.
   - Test `RiskPolicyEngine` risk scoring and `idempotency_key` deduplication.
   - Test `ContextEngine` multi-dimensional prompt assembly (including `ExecutionContext`).
   - Test task cancellation signal propagation.

2. **End-to-End Integration Tests**:
   - Execute an end-to-end multi-agent feature development task.
   - Simulate a worker crash during test execution to verify lease recovery.
   - Verify artifact offloading to file/object storage and LangSmith tracing integration.

---
