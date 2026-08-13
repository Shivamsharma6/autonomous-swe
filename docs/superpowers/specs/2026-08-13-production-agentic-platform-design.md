# AutoSWE Production Agentic Platform Design

**Date:** 2026-08-13

**Status:** Frozen and approved for implementation

**Deployment target:** Single-machine Docker Compose
**Memory system:** Existing external Unified Agent Memory System (UAMS)

## 1. Executive Summary

AutoSWE will become a production-grade, single-machine agentic software-engineering platform for Python and Node.js/TypeScript repositories. It will use a real checkpointed LangGraph runtime, a durable PostgreSQL scheduler, bounded parallel agents, typed internal communication, structured model outputs, governed tool execution, Docker-isolated Git worktrees, external UAMS memory, and evidence-backed verification.

The design deliberately separates responsibilities:

| Component | Question it answers | Authority |
| --- | --- | --- |
| Planner | What needs to happen? | Versioned task plan and DAG revisions |
| Scheduler | When and where may it happen? | Task lifecycle, leases, and concurrency admission |
| LangGraph | How does the reasoning workflow execute and resume? | Checkpointed graph execution |
| Agent runtime | How does an agent reason and request actions? | Typed model interaction loop |
| Tool gateway | What may the agent do? | Capabilities, risk, approval, and idempotency |
| Sandbox manager | Where may code safely execute? | Restricted container execution |
| Context engine | What does the agent need to know now? | Bounded task context |
| UAMS | What reusable knowledge should survive runs? | Durable cross-agent and cross-run memory |
| PostgreSQL | What operationally happened? | Domain state, messages, leases, audit, and metadata |
| Git | What source code exists? | Repository history and patches |
| Artifact store | What execution evidence was produced? | Immutable, content-hashed evidence |
| LangSmith/OpenTelemetry | How did execution behave? | Traces and operational telemetry |

This is production-grade within a single-host failure domain. The first release does not claim multi-host high availability.

## 2. Goals and Non-Goals

### 2.1 Goals

- Dynamically decompose engineering goals into validated typed task DAGs.
- Use LangGraph as the actual checkpointed graph runtime.
- Preserve independent scheduler and graph-execution state.
- Execute independent tasks in parallel under explicit resource limits.
- Resume safely after worker, process, Redis, or host-service interruption.
- Use UAMS as the only durable shared knowledge source.
- Persist typed agent-to-agent messages with at-least-once delivery.
- Require validated structured outputs at every model boundary.
- Let models select tools through typed tool calling.
- Run mutable engineering work in restricted Docker containers and isolated Git worktrees.
- Require human approval for commits, pushes, pull requests, infrastructure mutations, and deployments.
- Support Python and Node.js/TypeScript through extensible repository adapters.
- Provide deterministic CI, failure-injection coverage, correlated observability, SLOs, and recovery procedures.

### 2.2 Non-Goals for the First Release

- Kubernetes or multi-machine scheduling.
- Google A2A protocol interoperability.
- Autonomous merging or deployment without approval.
- Native support for every programming language.
- Bundling or duplicating the existing UAMS deployment.
- Treating Redis, LangGraph checkpoints, conversation transcripts, or local files as substitutes for UAMS knowledge.
- Maintaining the current fixed planner, SQLite runtime, raw-code parsing, host shell execution, hard-coded generated code, or disconnected scheduler as fallback paths.

## 3. Runtime Topology and State Ownership

### 3.1 Docker Compose Services

The core Compose profile contains:

- **api:** FastAPI control plane protected by a generated single-admin bearer token. It manages projects, runs, approvals, cancellation, status, artifacts, and task-scoped event streaming.
- **dispatcher:** Claims queued work using PostgreSQL leases, enforces concurrency policies, reconciles abandoned work, and dispatches resumable graph executions.
- **workers:** Execute compiled LangGraph workflows. Multiple worker processes support concurrent workflow runs and task subgraphs.
- **postgres:** Authoritative domain store and LangGraph checkpoint database.
- **redis:** Disposable wake-up and event transport using Redis Streams. Loss of Redis must not lose canonical state.
- **sandbox-manager:** The only service allowed to reach a constrained Docker socket proxy. It manages runner containers and Git worktrees.
- **docker-socket-proxy:** Exposes only the minimum Docker API required for allowlisted runner lifecycle operations.
- **web:** Operator dashboard.

The optional observability profile adds:

- OpenTelemetry Collector;
- Prometheus;
- Grafana.

UAMS remains external. AutoSWE connects through an environment-configured API endpoint such as `http://host.docker.internal:8000`; it never mounts the UAMS vault or directly accesses UAMS PostgreSQL or Qdrant.

### 3.2 State Ownership

- **PostgreSQL domain tables:** projects, repositories, runs, DAG revisions, tasks, attempts, leases, reservations, messages, tool executions, approvals, artifacts, audit events, outbox records, and consumer receipts.
- **LangGraph checkpoint tables:** current and historical resumable computation for each graph thread.
- **UAMS:** verified reusable semantic, episodic, and procedural knowledge across tasks, runs, and agents.
- **Git:** repository contents, branches, commits, and exported patches.
- **Artifact volume:** immutable logs, patches, test reports, coverage, screenshots, builds, and other evidence.
- **Redis:** delivery notification and live event transport only.

## 4. Domain, Scheduler, and Graph State

### 4.1 Typed Tasks

Every planned task has one `TaskType`:

```text
RESEARCH
IMPLEMENTATION
TEST
REFACTOR
DOCUMENTATION
VALIDATION
```

Every task contains:

- stable task ID and plan revision;
- title and bounded description;
- task type;
- dependencies;
- priority;
- assigned agent capability;
- acceptance criteria;
- allowed tools and risk ceiling;
- expected artifacts;
- retry and budget policy;
- estimated CPU, memory, model, sandbox, network, and wall-time requirements.

### 4.2 Separate State Machines

Task DAG and scheduler state is independent from LangGraph execution state.

Scheduler task states are:

```text
PENDING -> READY -> LEASED -> RUNNING
                         |-> BLOCKED
                         |-> COMPLETED
                         |-> FAILED
                         |-> CANCELLED
```

LangGraph execution states are:

```text
NOT_STARTED
RUNNING
WAITING_FOR_TOOL
WAITING_FOR_APPROVAL
WAITING_FOR_MEMORY
PAUSED
COMPLETED
FAILED
CANCELLED
NEEDS_RECONCILIATION
```

A scheduler task can remain `RUNNING` while its LangGraph execution is `WAITING_FOR_TOOL`, `WAITING_FOR_APPROVAL`, or `WAITING_FOR_MEMORY`. Scheduler `BLOCKED` is reserved for a dependency, conflict, exhausted policy, budget, or external condition that prevents progress; it does not represent an ordinary checkpointed graph suspension.

### 4.3 Reconciliation of Domain and Checkpoint State

Because domain updates and LangGraph checkpoint writes cross separate transaction boundaries, startup and periodic reconciliation applies this deterministic matrix:

| PostgreSQL domain state | LangGraph checkpoint | Resolution |
| --- | --- | --- |
| `RUNNING`, lease expired | Non-terminal | Reclaim and resume from the checkpoint |
| `RUNNING` | Completed | Idempotently finalize domain state from the verified graph result |
| `RUNNING` | Missing | Return to `READY`, or fail when retry/budget policy is exhausted |
| Terminal or cancelled | Non-terminal | Domain terminal state wins; never resume the checkpoint |
| Any | Wrong run, task, repository, or baseline identity | Quarantine as `NEEDS_RECONCILIATION` |
| Conflicting terminal outcomes | Conflicting result | Quarantine; never guess or overwrite evidence |

Reconciliation emits immutable audit events and is itself idempotent.

## 5. Planning, Scheduling, and Workflow Execution

### 5.1 Top-Level Graph

Each run executes this checkpointed flow:

1. **Intake:** validate repository identity, baseline, requested outcome, budgets, and cancellation state.
2. **Memory recall:** query UAMS for relevant project knowledge, procedures, prior decisions, and known failures.
3. **Repository analysis:** select the Python or Node/TypeScript adapter, index structure, read manifests, and record the baseline commit.
4. **Architect:** return a structured `TaskPlan` with typed tasks, dependencies, acceptance criteria, risk, tools, and budgets.
5. **Plan validation:** reject cycles, missing dependencies, invalid types, repository escapes, untestable acceptance criteria, unbounded task counts, and budget violations.
6. **Scheduler admission:** transactionally claim a bounded ready-task batch and reserve model/sandbox capacity.
7. **Dynamic fan-out:** emit LangGraph parallel sends only for scheduler-admitted tasks.
8. **Typed task subgraphs:** execute the subgraph selected by task type.
9. **Integration fan-in:** integrate successful outputs in dependency order, detect patch overlap, and turn conflicts into explicit repair work.
10. **Repository verification:** run adapter-selected lint, typecheck, tests, builds, and security checks.
11. **Bounded repair:** create or run debugger tasks using exact diffs, commands, outputs, artifacts, and prior attempts.
12. **Final review:** compare verified evidence against the original acceptance criteria and return a structured release decision.
13. **Approval:** interrupt before commits, pushes, pull requests, infrastructure mutations, or deployments.
14. **Completion:** export evidence, finalize domain state, and submit verified memory candidates for UAMS promotion.

### 5.2 Task-Type Subgraphs

```text
RESEARCH
  recall -> investigate -> evidence -> synthesis

IMPLEMENTATION
  recall -> implement -> targeted test -> review

TEST
  recall -> generate tests -> execute -> review

REFACTOR
  recall -> establish invariants -> refactor -> regression verify -> review

DOCUMENTATION
  recall -> draft -> validate links/examples -> review

VALIDATION
  recall -> inspect -> verify -> evidence
```

Task subgraphs return typed results and artifact references. They do not all pretend to be coding pipelines.

### 5.3 Concurrency Admission

The scheduler, not the planner or LangGraph, is authoritative for resource admission. Persisted limits include:

- `max_parallel_tasks`;
- `max_parallel_tasks_per_project`;
- `max_model_concurrency`;
- `max_sandbox_concurrency`.

The scheduler reserves capacity before dispatch and releases reservations transactionally on completion, cancellation, lease expiry, or reconciliation. LangGraph performs graph-level fan-out only after admission.

### 5.4 Dynamic DAG Mutation

Repair planning may append tasks but cannot arbitrarily expand the graph. Every mutation must satisfy:

- `max_dynamic_tasks`;
- `max_plan_depth`;
- `max_total_budget`;
- `max_total_execution_time`.

The new DAG is incrementally validated for cycles, dependency validity, project scope, acceptance criteria, and budget before the revision becomes current. Rejected mutations are recorded with reasons.

### 5.5 Replay-Safety Invariant

> Every node is replay-safe; every externally visible side effect is idempotent or protected by an explicit transactional/idempotency boundary.

Deterministic computation may replay. Side effects cannot be assumed restart-safe merely because they are inside a graph node.

## 6. Agent Runtime and Structured Outputs

### 6.1 Model Gateway

The model gateway exposes one typed OpenAI-compatible interface configurable for Ollama, Unsloth, and hosted providers. Provider settings and credentials come only from environment secrets. CI uses deterministic scripted models.

The gateway supports:

- structured-output schemas;
- native tool calling;
- streaming cancellation;
- timeout and retry classification;
- token and monetary accounting;
- model concurrency admission;
- trace correlation;
- provider capability detection.

### 6.2 Structured Boundaries

All model-facing outputs use versioned Pydantic models. Key schemas include:

- `TaskPlan` and `TaskPlanMutation`;
- `ResearchEvidence`;
- `ImplementationProposal`;
- `TestEvidence`;
- `ReviewFinding` and `ReviewDecision`;
- `ValidationResult`;
- `ToolCallRequest`;
- `ReleaseDecision`;
- `MemoryCandidate`.

Invalid output receives a bounded schema-repair loop. Exhaustion fails visibly. The runtime does not parse markdown code fences with regular expressions and does not substitute hard-coded example code.

### 6.3 Declarative Agent Specifications

Architect, Researcher, Coder, Tester, Reviewer, Debugger, Documentation, Validation, and Final Reviewer roles use the same runtime contract. A run instantiates only the roles required by its typed task DAG; it does not execute a fixed ceremonial roster.

Every versioned `AgentSpec` declares:

- role and purpose;
- accepted input and required output schemas;
- primary and fallback model policy;
- tool capability grants and maximum risk;
- context and UAMS retrieval policy;
- token, monetary, turn, and wall-time budgets;
- sandbox/network profile;
- retry, escalation, and termination rules.

The runtime validates the spec before execution and stores its immutable hash with every attempt, so an execution can be reproduced and audited against the exact agent policy used.

## 7. Typed Agent Communication

### 7.1 Message Contract

Internal messages use a versioned Pydantic discriminated union:

- `ContextHandoff`;
- `ResearchEvidence`;
- `PatchProposal`;
- `TestEvidence`;
- `ReviewFinding`;
- `ValidationResult`;
- `Blocker`;
- `TaskCompletion`.

Each message records sender, recipient, run, task, attempt, schema version, timestamp, causation ID, correlation ID, artifact references, and content hash. LangGraph state carries message IDs and compact summaries rather than unbounded message bodies.

### 7.2 At-Least-Once Delivery

Messages are committed to PostgreSQL with a transactional outbox record. Outbox publishers use `FOR UPDATE SKIP LOCKED` and publish to Redis Streams. Delivery is explicitly at least once: a crash before acknowledgement can redeliver an event.

Every event has a stable `event_id`. Consumers claim a unique `(consumer, event_id)` receipt before applying effects, making duplicate delivery harmless. Pending Redis messages are reclaimed after their acknowledgement deadline.

Delivery uses bounded exponential backoff with jitter. After eight unsuccessful attempts, the event enters a PostgreSQL dead-letter table containing payload, attempts, last error, and causation chain. Authenticated operators can inspect and replay dead letters.

### 7.3 Retention

Default retention is:

- Redis stream entries: 24 hours;
- full agent-message payloads: 90 days after terminal workflow state;
- resolved dead letters: 30 days;
- tool, approval, and audit metadata: 365 days;
- artifacts: project-configurable.

Cleanup never removes records referenced by an active workflow, unresolved approval, retained audit event, or current UAMS promotion.

## 8. UAMS Memory Integration

### 8.1 Boundary and Authority

AutoSWE integrates with the existing UAMS deployment through a narrow `MemoryPort`:

```text
ready()
get_context(task, project, budget)
search(query, entities, limit)
remember(candidate)
```

The production adapter uses the UAMS HTTP API; tests use an in-memory contract implementation. UAMS is the only durable cross-run knowledge store. LangGraph checkpoints are execution state. PostgreSQL messages and audit events record what happened but are not generalized memory.

Memory-dependent work pauses visibly when UAMS is unavailable. AutoSWE never silently substitutes conversation history, local notes, or a secondary vector store.

### 8.2 Knowledge-Promotion Gate

Agents create PostgreSQL `MemoryCandidate` records and cannot write UAMS directly. Promotion requires:

1. a verified task or workflow outcome;
2. a supported semantic, episodic, or procedural classification;
3. provenance and artifact evidence;
4. secret and PII redaction;
5. contradiction and duplication checks;
6. a minimum structural and evidence quality score;
7. human approval for identity, preference, security-sensitive, or cross-project knowledge.

Rejected candidates remain auditable but never enter UAMS. Raw prompts, transcripts, full logs, secrets, and speculative agent claims are never promoted.

### 8.3 Idempotent Promotion

Each accepted candidate receives a deterministic UUIDv5 derived from project, source run/task, candidate type, normalized content hash, and schema version. The UUID is embedded as UAMS YAML-frontmatter `memory_id`. Retries therefore address one logical memory rather than creating duplicates.

AutoSWE records the returned UAMS memory/revision identifiers before acknowledging the promotion outbox event. UAMS readiness and indexing state must confirm searchability before the promotion is considered complete.

### 8.4 Freshness and Provenance

Promoted memories include:

- `observed_at`, `verified_at`, and optional `valid_until`;
- repository identity and baseline commit;
- source run, task, attempt, and agent;
- originating message IDs and artifact hashes;
- verification commands and results;
- confidence and schema version;
- `supersedes` and `superseded_by` relationships when knowledge changes.

Retrieval rejects expired knowledge. Commit-scoped knowledge becomes stale when the repository baseline moves beyond its verified source without revalidation. Retrieved UAMS context retains memory, revision, source, and evidence identifiers.

## 9. Tool Governance, Approval, and Idempotency

### 9.1 Tool Registry

Every tool declares:

- versioned argument and result schemas;
- owning capabilities and eligible agents;
- risk classification rules;
- timeout and retry policy;
- replay policy;
- sandbox and network profile;
- externally visible side effects;
- approval requirements.

Agents cannot mutate files or run processes outside this registry.

### 9.2 Execution Protocol

The tool gateway:

1. validates the typed call and agent capability;
2. normalizes paths and rejects escape from the managed worktree;
3. calculates risk from the tool, arguments, repository policy, and target;
4. atomically claims the idempotency key;
5. creates an approval interrupt when required;
6. executes through the sandbox manager or isolated integration adapter;
7. persists a redacted result and evidence artifacts;
8. commits audit and outbox events.

Approval is bound to the exact normalized call hash, approver, expiry, repository, and baseline commit. Any argument or baseline change invalidates approval.

Commits, pushes, pull requests, infrastructure mutations, and deployments always require approval. An unknown side-effect outcome enters `NEEDS_RECONCILIATION` and is never blindly retried.

## 10. Sandbox and Repository Isolation

### 10.1 Worktree Isolation

Each mutable task receives an isolated managed Git worktree and branch. The source repository is imported read-only. Task outputs are patches, commits, logs, and evidence artifacts. Integration occurs in a separate worktree after dependency and overlap checks.

### 10.2 Container Policy

The sandbox manager allows only runner images pinned by digest and rejects:

- privileged mode;
- host networking;
- arbitrary host mounts;
- devices;
- Linux capability additions;
- root execution;
- unbounded output or processes.

Each runner uses:

- a read-only root filesystem;
- dropped capabilities and `no-new-privileges`;
- CPU, memory, PID, output, and wall-time limits;
- network disabled by default;
- explicit temporary, worktree, and artifact volumes;
- a persisted container ID controlled by cancellation.

Dependency installation uses an allowlisted egress profile and produces lockfile and network evidence. Command tools use argument arrays and never `shell=True`.

### 10.3 Actual Resource Accounting

Every attempt persists `SandboxExecution` telemetry:

```text
cpu_time_ms
peak_memory_bytes
peak_processes
processes_created
stdout_bytes
stderr_bytes
duration_ms
network_requests
network_bytes_sent
network_bytes_received
exit_code
exit_reason
limit_triggered
measurement_source
measurement_complete
```

CPU, memory, and peak PID measurements come from cgroups/Docker. Output is byte-counted with separate truncation metadata. Network usage comes from the controlled egress proxy. Exact lifetime process creation is recorded only when supported; otherwise `processes_created` is null and `measurement_complete` is false.

Observed resource use updates per-project and per-task-type rolling estimates. Scheduler and abuse controls enforce cumulative CPU, memory-time, network, output, model-token, monetary, and wall-time budgets.

## 11. Security Model

The first release uses a generated single-admin bearer token. Authorization interfaces remain separable so OIDC/RBAC can replace this adapter later.

Controls include:

- constant-time token comparison;
- explicit CORS origins;
- request-size and rate limits;
- no credentials in API responses;
- provider credentials supplied only as environment secrets;
- secret redaction before logs, messages, traces, artifacts, or UAMS candidates;
- repository and project identifier validation;
- canonical path and symlink containment;
- startup rejection of unsafe defaults;
- liveness distinct from readiness;
- immutable auth, approval, policy, tool, and administrative audit events;
- no committed tracing or model credentials.

## 12. Reliability and Observability

### 12.1 Correlation and Telemetry

Every request, run, task, graph execution, message, model call, tool call, sandbox, artifact, and UAMS operation carries correlated identifiers. Services emit redacted structured JSON logs.

Observability includes:

- LangSmith, when configured, for LangGraph and model traces;
- OpenTelemetry spans across API, dispatcher, worker, PostgreSQL, Redis, UAMS, tools, and sandbox execution;
- Prometheus metrics for queues, leases, task and graph states, retries, checkpoints, tokens, cost, tools, approvals, UAMS freshness, outbox lag, dead letters, sandbox resources, budgets, and SLOs;
- Grafana dashboards in the optional observability profile.

### 12.2 Time in State

Every workflow, task, approval, and UAMS-dependent operation persists `state_entered_at`. Transitions atomically record elapsed duration.

Prometheus histograms are:

- `task_state_duration_seconds{state,task_type,outcome}`;
- `workflow_state_duration_seconds{state,outcome}`;
- `approval_wait_duration_seconds{approval_type,outcome}`;
- `uams_wait_duration_seconds{operation,outcome}`.

Run, task, and project identifiers are not Prometheus labels. Per-task live state age comes from PostgreSQL timestamps, avoiding unbounded metric cardinality. Alerts cover prolonged `READY`, `LEASED`, `WAITING_FOR_MEMORY`, `WAITING_FOR_APPROVAL`, cancellation, and reconciliation states.

### 12.3 Reliability Rules

- Workers heartbeat persisted leases and stop claiming during graceful shutdown.
- Lease expiry is reconciled with the referenced checkpoint before reclaim.
- Retry classes are transient, permanent, policy, budget, cancellation, and uncertain-side-effect.
- PostgreSQL migrations run as a dedicated locked startup job.
- Readiness verifies PostgreSQL, Redis, checkpoint schema, sandbox manager, model configuration, and UAMS readiness.
- Cancellation persists first, broadcasts second, is checked at every boundary, and terminates active runner containers.
- Startup reconciliation repairs outbox lag, stale leases, abandoned reservations, state divergence, and interrupted UAMS promotions.

### 12.4 Initial Single-Machine SLOs

| Objective | Initial target |
| --- | ---: |
| API availability | At least 99.5% over 30 days, excluding declared maintenance |
| Eligible-task dispatch latency | p95 under 5 seconds; p99 under 15 seconds when capacity is available |
| Checkpoint write latency | p99 under 2 seconds |
| Acknowledged checkpoint durability | Zero acknowledged state transitions lost |
| Outbox/event delivery latency | p99 under 2 seconds while PostgreSQL and Redis are healthy |
| Approval notification latency | p95 under 5 seconds after persistence |
| Cancellation observed by worker | p95 under 5 seconds |
| Active sandbox termination after cancellation | p99 under 15 seconds |
| Artifact integrity | 100% SHA-256 verification on storage and retrieval |
| Worker-crash recovery | p95 under 45 seconds from lost heartbeat to resumable claim |
| UAMS context retrieval | p95 under 5 seconds while UAMS reports ready |
| UAMS promotion visibility | p95 under 60 seconds from acceptance to searchable memory |

An eligible task excludes dependency-blocked, approval-waiting, budget-exhausted, capacity-saturated, and administratively paused work. SLO dashboards display objective, observed value, error-budget use, and burn rate. Benchmarking may tighten targets or define a hardware minimum; weakening a target requires a recorded design decision.

## 13. Operator Experience

The dashboard distinguishes:

- Task DAG state from LangGraph execution state;
- scheduler reservations from actual use;
- agent messages from Redis transport events;
- pending approvals from blocked tasks;
- current plan revision from dynamic repair additions;
- verified artifacts from agent claims;
- current state age and blocking reason.

Operators can inspect and replay dead letters, cancel runs, approve exact tool hashes, view checkpoint history, download verified artifacts, inspect sandbox resource use, and see why a task is not progressing.

## 14. Repository Adapters

The first release contains adapters for:

- Python projects using `pyproject.toml`, `requirements.txt`, or recognized environment metadata;
- Node.js/TypeScript projects using `package.json` and detected package-manager lockfiles.

Adapters define safe discovery, source/test paths, dependency installation, lint, typecheck, test, build, and artifact collection. Repository commands are inferred from manifests and policy, not composed from unconstrained model strings. The registry is extensible without changing scheduler or graph semantics.

## 15. Testing Strategy

CI is deterministic and does not require a live LLM, UAMS, LangSmith, or public network.

### 15.1 Test Layers

- Unit tests for schemas, DAG validation, scheduling, admission, budgets, state transitions, message deduplication, promotion gates, policy, and path containment.
- Property-based tests for generated DAG cycles, dependencies, mutation limits, and concurrency invariants.
- Contract tests for model, UAMS, sandbox, artifact, event-bus, and checkpointer adapters.
- Integration tests with real PostgreSQL, Redis, LangGraph checkpointing, outbox delivery, dead letters, leases, approvals, and cancellation.
- Sandbox tests for escape prevention, disabled networking, limits, output truncation, telemetry, termination, and forbidden Docker configuration.
- Security tests for authentication, CORS, redaction, malformed structured output, injection, symlinks, approval binding, replay, and cross-project isolation.
- Python and Node.js/TypeScript fixture repositories for detection, targeted work, integration, and full verification.
- Optional nightly tests for configured real models and external UAMS. These cannot mask deterministic failures.

### 15.2 Domain/Checkpoint Divergence

Integration tests inject every reconciliation-matrix mismatch, run reconciliation twice, and prove deterministic idempotent outcomes. This explicitly tests the risk created by PostgreSQL domain state and LangGraph checkpoint state being separate authorities.

### 15.3 Mandatory Branching-and-Repair E2E

The release E2E uses scripted structured model responses through the real planner, scheduler, LangGraph, tool gateway, sandbox, Git, and artifact pipeline:

```text
Structured plan
  |- Research task ------|
  |- Implementation task |-- bounded parallel execution
  |- Test task ----------|
              |
       persisted handoffs
              |
       patch integration
              |
    intentional test failure
              |
      debugger repair task
              |
   full repository verification
              |
   exact-call approval interrupt
              |
     approved Git operation
              |
    UAMS promotion candidate
```

The scenario verifies checkpoints, reservations, messages, dynamic-mutation limits, repair evidence, cancellation boundaries, and replay safety rather than checking only the final status.

### 15.4 Failure Injection

Tests terminate workers during model calls, checkpoints, tool execution, outbox publication, integration, and UAMS promotion. They stop Redis, make UAMS temporarily unavailable, expire leases, duplicate events, corrupt artifacts, and simulate unknown side-effect outcomes. Each test proves the documented recovery state and absence of duplicate effects.

### 15.5 Artifact Integrity

For every evidence artifact, tests recompute the stored bytes' SHA-256 and compare it with PostgreSQL metadata. Deliberately corrupted artifacts must:

- fail verification;
- become `CORRUPT`;
- emit an audit and alert event;
- be excluded from context and final-review evidence;
- never be presented as valid without an explicit forensic override.

## 16. Migration and Recovery

### 16.1 Migration Sequence

1. Introduce domain ports, typed models, migrations, and deterministic adapters.
2. Add PostgreSQL, Redis, and real LangGraph execution behind a disabled-by-default rollout switch.
3. Migrate API and dashboard reads to authoritative domain state.
4. Run compatibility tests for supported project/task API behavior.
5. Enable the new engine by default.
6. Remove SQLite, fixed planning, raw-code parsing, host `shell=True`, hard-coded generation, and disconnected scheduling.
7. Mark the old design superseded and publish operator, recovery, security, UAMS, and extension guides.

There is no legacy execution fallback after cutover.

### 16.2 Schema and Deployment Recovery

Every deployment runs migration preflight, takes a verified PostgreSQL backup, records schema and application versions, and acquires a migration lock.

- Reversible schema changes have tested Alembic upgrades and downgrades.
- Destructive and data changes use expand-contract migrations.
- The previous application image remains compatible during the rollback window.
- A failed forward migration stops before workers start.
- When safe downgrade is impossible, recovery restores the verified backup into a clean PostgreSQL volume and starts the previous pinned image.
- Artifact volumes have independent manifests and hashes.

Acceptance tests migrate a populated database, verify domain/checkpoint/artifact integrity, roll back or restore, start the previous release, and resume an existing workflow.

## 17. Compose Delivery

The repository ships:

- core and observability Compose profiles;
- pinned service and runner images;
- health and readiness probes;
- persistent PostgreSQL, Redis, artifact, and managed-worktree volumes;
- secret templates without secret values;
- an initialization command that generates the admin token;
- migration, backup, restore, and reconciliation commands;
- a single operator smoke command;
- documented UAMS external configuration;
- resource limits for every service.

## 18. Release Acceptance Gate

The first production release is accepted only when it demonstrates all of the following:

1. Dynamic structured planning containing at least three task types.
2. Bounded parallel execution with visible scheduler reservations.
3. Worker termination followed by checkpointed recovery.
4. Deterministic domain/checkpoint divergence reconciliation.
5. Persisted typed inter-agent handoffs with duplicate-safe at-least-once delivery.
6. An approval-bound commit or pull-request action.
7. Cancellation of an active sandbox.
8. UAMS recall and gated idempotent promotion with freshness/provenance.
9. Python and TypeScript repository workflows.
10. Branching, intentional failure, debugger repair, and final verification in the deterministic E2E.
11. Duplicate outbox publication without duplicate effects.
12. Artifact metadata-to-object SHA-256 verification and corrupt-artifact rejection.
13. Measured SLO results and complete resource accounting.
14. Full deterministic, integration, failure, and security suites passing.
15. Tested database migration rollback or backup restoration with prior-release workflow resume.
16. A fresh machine completing the Compose runbook without manual database edits.

## 19. Reference Contracts

- LangGraph checkpoints are used according to the current [LangGraph persistence model](https://docs.langchain.com/oss/python/langgraph/persistence).
- Parallel graph execution follows LangGraph fan-out/fan-in semantics while scheduler admission remains authoritative: [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api).
- Structured model responses use schema validation rather than text parsing: [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output).
- UAMS lifecycle is `ready/begin or context/search/remember`, with distilled writes, stable memory IDs, current-revision evidence, and readiness verification.
