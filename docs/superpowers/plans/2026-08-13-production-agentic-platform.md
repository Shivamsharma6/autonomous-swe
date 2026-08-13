# Production Agentic Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before claiming a phase complete. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prototype execution paths with one production-authoritative, checkpointed agentic software-engineering platform that dynamically plans typed DAGs, schedules bounded parallel work, persists durable state and messages, uses external UAMS for shared memory, governs tool calls, and runs Python and Node.js/TypeScript work in isolated containers.

**Architecture:** PostgreSQL owns domain state and scheduler admission; LangGraph with `PostgresSaver` owns resumable graph execution; Redis Streams transports disposable wake-ups and at-least-once events backed by a PostgreSQL transactional outbox; external UAMS is the sole durable cross-run memory; a typed OpenAI-compatible gateway is the only model boundary; a policy-enforcing tool gateway delegates repository commands to a constrained Docker sandbox manager. The FastAPI control plane and dispatcher are separate processes, and the entire local platform is delivered through single-machine Docker Compose.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, Alembic, PostgreSQL 16, Redis 7 Streams, LangGraph plus `langgraph-checkpoint-postgres`, OpenAI-compatible HTTP APIs, HTTPX, Docker Engine API, Git worktrees, Prometheus/OpenTelemetry, pytest, pytest-asyncio, Hypothesis, testcontainers, Ruff, MyPy, Docker Compose.

**Source design:** `docs/superpowers/specs/2026-08-13-production-agentic-platform-design.md`

**Execution policy:** Complete tasks in order. Within each task, write the failing test first, run the stated narrow test and observe the expected failure, implement the smallest complete production behavior, rerun the narrow test, then run the phase regression command. Never keep the legacy SQLite, fixed-planner, raw-code-parser, host-shell, hard-coded-code, or disconnected-scheduler paths as fallbacks.

---

## Phase 1: Contracts and Configuration

### Task 1: Pin the production dependency and quality baseline

**Files:**
- Modify: `pyproject.toml`
- Create: `requirements.lock`
- Create: `.env.example`
- Create: `tests/unit/test_settings.py`
- Create: `infrastructure/config.py`

- [x] Write `tests/unit/test_settings.py` proving startup rejects a missing admin token, SQLite URLs, wildcard CORS, an unconfigured model base URL, and an unconfigured UAMS URL in production mode. Prove the same settings accept explicit test adapters only when `AUTOSWE_ENV=test`.
- [x] Run `pytest tests/unit/test_settings.py -q`; expect import failure for `infrastructure.config.Settings`.
- [x] Add pinned-compatible runtime dependencies for SQLAlchemy async, AsyncPG, Alembic, Redis asyncio, HTTPX, psycopg, LangGraph Postgres checkpoints, Docker, Prometheus, OpenTelemetry, structured logging, and multipart handling. Add development groups for pytest-asyncio, Hypothesis, testcontainers, Ruff, and MyPy.
- [x] Implement a Pydantic settings model with `SecretStr` credentials and explicit validated fields for PostgreSQL, Redis, UAMS, model gateway, artifact root, managed-worktree root, CORS origins, service limits, and sandbox image digests. Do not expose secret values in `repr`, JSON, logs, or API schemas.
- [x] Put names and safe local examples—not secret values—in `.env.example`. Remove any committed model or tracing credential from Compose during Task 15.
- [x] Generate `requirements.lock` from the chosen resolved environment and install from it in CI and images.
- [x] Run `pytest tests/unit/test_settings.py -q`, then `ruff check infrastructure tests/unit/test_settings.py`.
- [x] Commit: `build: establish production dependencies and settings`

### Task 2: Define versioned domain, graph, model, and message contracts

**Files:**
- Create: `domain/__init__.py`
- Create: `domain/enums.py`
- Create: `domain/models.py`
- Create: `domain/messages.py`
- Create: `domain/events.py`
- Create: `tests/unit/domain/test_models.py`
- Create: `tests/unit/domain/test_messages.py`
- Modify: `pyproject.toml`

- [x] Write tests for the six `TaskType` values; separate scheduler and graph execution enums; legal transition tables; immutable IDs; budget fields; task dependencies; plan revisions; `AgentSpec` hashing; typed results; `SandboxExecution`; `MemoryCandidate`; approval call hashes; and artifact lifecycle states including `CORRUPT`.
- [x] Write parametrized serialization tests for the discriminated message union: `ContextHandoff`, `ResearchEvidence`, `PatchProposal`, `TestEvidence`, `ReviewFinding`, `ValidationResult`, `Blocker`, and `TaskCompletion`. Verify every message carries schema version, sender, recipient, run/task/attempt IDs, timestamp, causation/correlation IDs, artifact references, and content SHA-256.
- [x] Run `pytest tests/unit/domain -q`; expect failures because `domain` does not exist.
- [x] Implement strict Pydantic models with `extra="forbid"`, timezone-aware UTC timestamps, bounded collection sizes, UUID identifiers, non-negative budgets, and canonical SHA-256 helpers. Define `TaskPlan`, `TaskPlanMutation`, `ReleaseDecision`, and all model-facing outputs as versioned structured schemas.
- [x] Add `domain*` to package discovery.
- [x] Run `pytest tests/unit/domain -q` and `mypy domain`.
- [x] Commit: `feat: define typed platform contracts`

### Task 3: Validate task DAGs and bounded dynamic mutations

**Files:**
- Create: `planning/__init__.py`
- Create: `planning/validator.py`
- Create: `tests/unit/planning/test_validator.py`
- Create: `tests/property/test_dag_properties.py`
- Modify: `pyproject.toml`

- [x] Write examples covering missing dependencies, self-dependencies, cycles, duplicate task IDs, invalid repository scope, missing acceptance criteria, unsupported tools, depth overflow, total-budget overflow, and total-execution-time overflow.
- [x] Write Hypothesis properties proving an accepted DAG is acyclic, every dependency exists, no admitted mutation changes an existing task, and all accepted mutations stay under `max_dynamic_tasks`, `max_plan_depth`, `max_total_budget`, and `max_total_execution_time`.
- [x] Run `pytest tests/unit/planning tests/property/test_dag_properties.py -q`; expect import failure.
- [x] Implement Kahn topological validation and incremental mutation validation. Return structured validation issues; do not partially mutate the current revision on rejection.
- [x] Run the narrow tests twice with a fixed Hypothesis seed to prove repeatability.
- [x] Commit: `feat: validate dynamic task dags and repair bounds`

---

## Phase 2: PostgreSQL Authority, Migrations, and Artifacts

### Task 4: Create the authoritative PostgreSQL schema and repositories

**Files:**
- Create: `persistence/__init__.py`
- Create: `persistence/database.py`
- Create: `persistence/tables.py`
- Create: `persistence/repositories.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_production_domain.py`
- Create: `tests/integration/persistence/test_repositories.py`
- Create: `tests/integration/persistence/test_migrations.py`
- Modify: `pyproject.toml`

- [x] Write integration tests against real PostgreSQL for projects, repositories, runs, plan revisions, tasks, attempts, leases, reservations, graph-execution metadata, messages, outbox records, consumer receipts, dead letters, tool executions, approvals, artifacts, memory candidates, sandbox executions, state durations, and immutable audit events.
- [x] Prove compare-and-set transitions reject stale revisions; terminal states cannot regress; a task transition and its audit/outbox records commit atomically; and repository methods always require project scope.
- [x] Run `pytest tests/integration/persistence -q`; expect import or connection fixture failures.
- [x] Implement SQLAlchemy async tables with PostgreSQL enums, UUID primary keys, UTC timestamps, check constraints, unique idempotency keys, content-hash constraints, foreign keys, and indexes for ready work, expired leases, unpublished outbox events, pending approvals, state age, and unresolved dead letters.
- [x] Implement a transaction boundary object. Repository methods accept an existing session so orchestration services can update state, audit, and outbox atomically.
- [x] Implement Alembic upgrade and downgrade. Run upgrade on an empty database, populate it, downgrade, upgrade again, and verify retained compatible records.
- [x] Run `pytest tests/integration/persistence -q` and `alembic check`.
- [x] Commit: `feat: add authoritative postgres domain store`

### Task 5: Build content-addressed artifact storage with corruption quarantine

**Files:**
- Create: `persistence/artifacts.py`
- Create: `tests/unit/persistence/test_artifacts.py`
- Create: `tests/integration/persistence/test_artifact_integrity.py`

- [x] Write tests that store bytes under a SHA-256-derived path, persist metadata, verify metadata-to-object hashes on every retrieval, reject path traversal and symlinks, deduplicate identical bytes, and atomically finalize temporary writes.
- [x] Write the acceptance test that mutates stored bytes, then proves retrieval marks metadata `CORRUPT`, emits an audit/outbox event, excludes the object from an evidence query, and cannot return it as valid evidence.
- [x] Run the tests and observe import failure.
- [x] Implement `ArtifactStore.put`, `verify`, `open_verified`, and `quarantine`. Use filesystem primitives without following user-controlled symlinks and never trust a client-supplied digest.
- [x] Run `pytest tests/unit/persistence/test_artifacts.py tests/integration/persistence/test_artifact_integrity.py -q`.
- [x] Commit: `feat: enforce artifact integrity and quarantine`

---

## Phase 3: Scheduler and Durable Communication

### Task 6: Implement authoritative scheduler admission, leases, and accounting

**Files:**
- Replace: `execution/scheduler/scheduler.py`
- Create: `execution/scheduler/service.py`
- Create: `execution/scheduler/reconciliation.py`
- Create: `tests/unit/scheduler/test_admission.py`
- Create: `tests/integration/scheduler/test_leases.py`
- Create: `tests/integration/scheduler/test_reconciliation.py`

- [x] Write unit tests for dependency readiness and all four admission ceilings: total parallel tasks, per-project parallel tasks, model concurrency, and sandbox concurrency.
- [x] Write concurrent PostgreSQL tests proving `FOR UPDATE SKIP LOCKED` gives one lease to one dispatcher, reservations cannot oversubscribe, heartbeats extend only the owning lease, expired leases release reservations once, cancellation persists before notification, and retries honor category and budget.
- [x] Encode the full domain/checkpoint reconciliation matrix as parametrized integration tests. Run reconciliation twice for every case and assert identical final state and one logical audit effect.
- [x] Run the three scheduler test files and observe failures against the current in-memory scheduler.
- [x] Implement transactional ready-task selection, lease tokens, heartbeats, reservation acquisition/release, cancellation, retry classification, rolling resource estimates, and deterministic reconciliation hooks. Scheduler admission remains authoritative; graph fan-out receives only admitted task IDs.
- [x] Run scheduler tests plus `pytest tests/unit/planning -q`.
- [x] Commit: `feat: add durable bounded task scheduler`

### Task 7: Implement transactional outbox and at-least-once Redis Streams delivery

**Files:**
- Create: `messaging/__init__.py`
- Create: `messaging/outbox.py`
- Create: `messaging/redis_streams.py`
- Create: `messaging/consumer.py`
- Create: `messaging/retention.py`
- Create: `tests/unit/messaging/test_consumer.py`
- Create: `tests/integration/messaging/test_delivery.py`
- Create: `tests/integration/messaging/test_dead_letters.py`

- [ ] Write tests proving the publisher claims outbox rows with `SKIP LOCKED`, persists publish attempts, and can publish the same stable event twice after a crash.
- [ ] Prove consumers atomically claim unique `(consumer, event_id)` receipts before applying an effect, duplicate delivery creates no duplicate effect, pending entries are reclaimed, exponential retry is bounded, and the eighth failed delivery becomes a PostgreSQL dead letter with its causation chain.
- [ ] Prove retention leaves active workflows, unresolved approvals, current UAMS promotions, and referenced audit evidence untouched while enforcing the approved 24-hour/90-day/30-day/365-day policies.
- [ ] Run tests; expect missing package failures.
- [ ] Implement Redis Streams as transport only. PostgreSQL message/outbox/receipt rows remain canonical, and Redis loss is recoverable by republishing unpublished or unacknowledged canonical events.
- [ ] Run `pytest tests/unit/messaging tests/integration/messaging -q`.
- [ ] Commit: `feat: add duplicate-safe agent event delivery`

---

## Phase 4: External UAMS and Model Runtime

### Task 8: Replace SQLite memory with the external UAMS port and promotion gate

**Files:**
- Create: `knowledge/memory/port.py`
- Create: `knowledge/memory/uams.py`
- Create: `knowledge/memory/fake.py`
- Replace: `knowledge/memory/storage.py`
- Create: `knowledge/memory/promotion.py`
- Create: `tests/contract/test_memory_port.py`
- Create: `tests/unit/memory/test_promotion.py`
- Create: `tests/integration/memory/test_uams_failure.py`

- [ ] Define one contract suite and run it against the deterministic in-memory adapter and an HTTP mock of external UAMS for `ready`, `get_context`, `search`, and `remember`.
- [ ] Prove retrieval preserves memory/revision/provenance IDs, rejects expired knowledge, marks commit-scoped knowledge stale after baseline movement, and never silently falls back when UAMS is unavailable.
- [ ] Test the promotion gate for verified outcome, classification, evidence, redaction, contradiction/duplicate checks, quality score, and approval-sensitive knowledge. Test UUIDv5 stability from project/source/type/content/schema.
- [ ] Prove a crash after UAMS write but before local acknowledgement retries the same `memory_id`; promotion is complete only after UAMS reports the current revision searchable.
- [ ] Run tests against the existing SQLite implementation and observe failures.
- [ ] Implement the narrow `MemoryPort`, HTTPX UAMS adapter, test fake, promotion service, freshness/provenance metadata, and visible `WAITING_FOR_MEMORY` graph transitions. Delete SQLite connection and schema code from `knowledge/memory/storage.py`; retain only compatibility imports that point to the port for one commit, then remove those imports in Task 18.
- [ ] Run `pytest tests/contract/test_memory_port.py tests/unit/memory tests/integration/memory -q`.
- [ ] Commit: `feat: integrate external uams as durable memory`

### Task 9: Build the typed OpenAI-compatible model gateway and declarative agents

**Files:**
- Create: `agents/gateway.py`
- Replace: `agents/base.py`
- Create: `agents/specs.py`
- Create: `agents/scripted.py`
- Create: `tests/contract/test_model_gateway.py`
- Create: `tests/unit/agents/test_runtime.py`

- [ ] Write contract tests for JSON-schema structured output, native tool calls, streaming cancellation, provider capability detection, retry classification, token/cost accounting, trace IDs, timeouts, and concurrency admission.
- [ ] Write bounded schema-repair tests: one invalid response followed by a valid response succeeds and records both attempts; exhaustion fails visibly without regex extraction or hard-coded substitute output.
- [ ] Write tests proving immutable `AgentSpec` hashes cover role, input/output schema versions, model policy, grants/risk, memory policy, all budgets, sandbox/network profile, and termination rules.
- [ ] Run tests and observe current agents cannot satisfy the contract.
- [ ] Implement an HTTPX OpenAI-compatible gateway plus a deterministic scripted gateway. Implement one generic agent runtime that validates its `AgentSpec`, gathers bounded context, invokes the gateway, validates typed output, dispatches typed tool calls, and records usage.
- [ ] Convert architect, researcher, coder, tester, reviewer, debugger, documentation, validation, and final-review modules into declarative specs over the shared runtime; instantiate only roles required by the DAG.
- [ ] Run `pytest tests/contract/test_model_gateway.py tests/unit/agents -q`.
- [ ] Commit: `feat: add structured model and agent runtime`

---

## Phase 5: LangGraph Execution

### Task 10: Build typed task subgraphs and the checkpointed top-level graph

**Files:**
- Create: `workflows/state.py`
- Create: `workflows/task_subgraphs.py`
- Replace: `workflows/feature.py`
- Create: `workflows/checkpoints.py`
- Create: `workflows/runtime.py`
- Create: `tests/unit/workflows/test_task_routing.py`
- Create: `tests/integration/workflows/test_postgres_checkpoints.py`
- Create: `tests/integration/workflows/test_parallel_fanout.py`

- [ ] Write routing tests for the approved subgraphs: research, implementation, test, refactor, documentation, and validation. Assert each uses only its required nodes and returns typed result/message/artifact IDs.
- [ ] Write real PostgresSaver tests proving a stable thread identity, checkpoint resume after process recreation, graph states distinct from scheduler states, and explicit interrupts for tool, approval, and UAMS waits.
- [ ] Write a fan-out test with three independent admitted tasks and one dependent integration task. Assert max observed concurrency equals configured admission, result ordering is deterministic by task ID at fan-in, and non-admitted tasks never execute.
- [ ] Run tests and observe failures against `MemorySaver` and the fixed graph.
- [ ] Implement the top-level intake/recall/analyze/architect/validate/admit/send/fan-in/verify/repair/review/approval/complete graph. Compile production graphs only with PostgresSaver. Nodes pass compact typed state and durable IDs, not unbounded bodies.
- [ ] Ensure every side-effecting node uses an outbox/idempotency or explicit uncertain-outcome boundary and checks cancellation before and after I/O.
- [ ] Run `pytest tests/unit/workflows tests/integration/workflows -q`.
- [ ] Commit: `feat: execute typed workflows with postgres checkpoints`

### Task 11: Implement bounded repair mutation and deterministic release review

**Files:**
- Create: `workflows/repair.py`
- Create: `workflows/review.py`
- Create: `tests/unit/workflows/test_repair.py`
- Create: `tests/integration/workflows/test_repair_resume.py`

- [ ] Write tests where repository verification fails, the debugger emits a typed mutation, incremental validation adds one repair task, and the next verification succeeds. Cover mutation count, depth, budget, time, repeated-signature, and no-progress termination.
- [ ] Prove a crash after accepting the mutation but before dispatch resumes the exact plan revision without adding the repair twice.
- [ ] Prove final review uses only verified non-corrupt artifact IDs and maps every original acceptance criterion to explicit evidence or a failure reason.
- [ ] Run tests, implement the repair controller and release reviewer, then rerun.
- [ ] Commit: `feat: add bounded evidence-driven repair workflow`

---

## Phase 6: Tool Gateway, Repository Adapters, and Sandbox

### Task 12: Implement capability, risk, approval, and idempotent tool governance

**Files:**
- Replace: `tools/gateway.py`
- Replace: `tools/registry.py`
- Replace: `policies/risk.py`
- Create: `tools/approval.py`
- Create: `tests/unit/tools/test_policy.py`
- Create: `tests/integration/tools/test_idempotency.py`
- Create: `tests/security/test_approval_binding.py`

- [ ] Write tests for versioned argument/result schemas, capability ownership, eligible agents, risk calculation, timeout/retry/replay policies, path containment, side-effect declaration, and mandatory approval for commit/push/PR/infra/deploy.
- [ ] Prove the gateway atomically claims a stable idempotency key; a replay returns the persisted result; concurrent calls execute once; and an uncertain side effect becomes `NEEDS_RECONCILIATION` rather than retrying.
- [ ] Prove approval is bound to normalized arguments, repository, baseline commit, approver, and expiry. Any difference invalidates it.
- [ ] Run tests against the current gateway, implement the registry/gateway/approval service, and rerun.
- [ ] Commit: `feat: govern typed tool calls and approvals`

### Task 13: Add Python and Node.js/TypeScript repository adapters

**Files:**
- Create: `execution/repositories/__init__.py`
- Create: `execution/repositories/base.py`
- Create: `execution/repositories/python.py`
- Create: `execution/repositories/node.py`
- Create: `execution/repositories/registry.py`
- Create: `tests/fixtures/python_project/pyproject.toml`
- Create: `tests/fixtures/node_project/package.json`
- Create: `tests/fixtures/node_project/package-lock.json`
- Create: `tests/unit/repositories/test_adapters.py`

- [ ] Write fixture tests for detection, source/test discovery, lockfile selection, dependency command, lint, typecheck, targeted test, full test, build, and artifact collection.
- [ ] Prove every command is an argument array selected from manifest/adapter policy; reject model-provided shell strings, ambiguous lockfiles, repository escapes, and unsupported lifecycle scripts.
- [ ] Run tests, implement the adapter protocol and two adapters, then rerun.
- [ ] Commit: `feat: add governed python and node repository adapters`

### Task 14: Replace host shell execution with isolated worktree containers

**Files:**
- Replace: `execution/sandbox/runner.py`
- Create: `execution/sandbox/manager.py`
- Create: `execution/sandbox/policy.py`
- Create: `execution/sandbox/worktrees.py`
- Create: `tests/unit/sandbox/test_policy.py`
- Create: `tests/integration/sandbox/test_container.py`
- Create: `tests/security/test_sandbox_escape.py`

- [ ] Write policy tests rejecting privileged mode, host network, arbitrary mounts, devices, capabilities, root, mutable rootfs, unpinned images, unrestricted egress, unbounded PIDs/output/time, and commands that are not argument arrays.
- [ ] Write Git worktree tests for one branch/worktree per mutable task, read-only source repository import, separate integration worktree, normalized paths, symlink escape prevention, and deterministic cleanup after terminal state.
- [ ] With the controlled Docker integration fixture, prove network disabled by default, cancellation terminates the persisted container ID, output truncation is byte-accurate, and CPU/memory/PID/time limits produce explicit exit reasons.
- [ ] Verify persisted telemetry fields: CPU time, peak memory, peak processes, processes created or null, stdout/stderr bytes, duration, network counts/bytes, exit code/reason, triggered limit, measurement source, and completeness.
- [ ] Run sandbox tests, remove every `shell=True` and host command execution path, implement the Docker Engine adapter and worktree manager, then rerun.
- [ ] Commit: `feat: isolate repository work in constrained containers`

---

## Phase 7: Control Plane, Services, and Compose

### Task 15: Split service entry points and secure the FastAPI control plane

**Files:**
- Replace: `apps/api/main.py`
- Replace: `apps/api/routes.py`
- Replace: `apps/api/schemas.py`
- Replace: `apps/api/websocket.py`
- Replace: `apps/worker/runner.py`
- Create: `apps/dispatcher/main.py`
- Create: `apps/api/auth.py`
- Create: `apps/api/dependencies.py`
- Create: `tests/unit/api/test_auth.py`
- Create: `tests/integration/api/test_control_plane.py`
- Create: `tests/security/test_api_security.py`

- [ ] Test constant-time single-admin bearer authentication, explicit CORS, request-size limits, rate limits, secret-free responses, validated IDs, scoped artifact downloads, exact-call approval, cancellation, dead-letter inspection/replay, liveness, dependency-aware readiness, and task-scoped event streams.
- [ ] Prove API process startup does not launch dispatcher/worker loops; dispatcher claims leases; workers only execute dispatched graph threads; graceful shutdown stops claims and preserves resumability.
- [ ] Run tests and observe failures against the monolithic background-task API.
- [ ] Implement separate dependency-injected application factories and process entry points. Readiness checks PostgreSQL, Redis, checkpoint schema, sandbox manager, model configuration, and external UAMS; liveness checks only the process.
- [ ] Run `pytest tests/unit/api tests/integration/api tests/security/test_api_security.py -q`.
- [ ] Commit: `feat: separate and secure platform services`

### Task 16: Deliver the single-machine Compose stack and operator commands

**Files:**
- Replace: `docker-compose.yml`
- Create: `docker-compose.observability.yml`
- Create: `Dockerfile`
- Create: `infrastructure/docker-socket-proxy.env`
- Create: `scripts/init-admin-token.sh`
- Create: `scripts/migrate.sh`
- Create: `scripts/backup.sh`
- Create: `scripts/restore.sh`
- Create: `scripts/reconcile.sh`
- Create: `scripts/smoke.sh`
- Create: `tests/compose/test_compose_config.py`

- [ ] Write static tests that parse the rendered Compose model and require api, dispatcher, workers, postgres, redis, sandbox-manager, docker-socket-proxy, and web. Assert health checks, resource limits, restart policies, pinned images, persistent volumes, internal networks, no broad Docker socket mount, and external UAMS configured only by URL/token.
- [ ] Assert Compose contains no credential values and specifically no committed LangSmith/model/admin/UAMS secret. Remove the currently committed tracing key from `docker-compose.yml`; report that the user must rotate it because deleting it from the current file cannot erase Git history.
- [ ] Implement admin-token generation with restrictive permissions; locked migrations; verified PostgreSQL backup/restore; reconciliation; and one smoke command that initializes, migrates, starts, checks readiness, runs a deterministic workflow, verifies artifacts, and shuts down cleanly.
- [ ] Validate with `docker compose config --quiet` and `pytest tests/compose -q`.
- [ ] Commit: `ops: deliver production single-host compose stack`

---

## Phase 8: Observability, Failure Injection, and Cutover

### Task 17: Add correlated telemetry, time-in-state metrics, and SLOs

**Files:**
- Replace: `observability/metrics.py`
- Replace: `observability/tracing.py`
- Create: `observability/logging.py`
- Create: `observability/slo.py`
- Create: `infrastructure/otel-collector.yaml`
- Create: `infrastructure/prometheus.yml`
- Create: `infrastructure/grafana/dashboards/platform.json`
- Create: `infrastructure/grafana/provisioning/dashboards.yaml`
- Create: `infrastructure/grafana/provisioning/datasources.yaml`
- Create: `tests/unit/observability/test_metrics.py`
- Create: `tests/integration/observability/test_state_durations.py`

- [ ] Test correlation propagation across request/run/task/graph/message/model/tool/sandbox/artifact/UAMS spans and redaction in logs, traces, and metric attributes.
- [ ] Test atomic `state_entered_at` duration recording and exact histogram names: `task_state_duration_seconds`, `workflow_state_duration_seconds`, `approval_wait_duration_seconds`, and `uams_wait_duration_seconds`. Assert no run/task/project IDs are Prometheus labels.
- [ ] Encode all approved initial SLO definitions, eligibility rules, burn-rate calculations, and alert thresholds. Add dashboards for state age/blocking reason, reservations versus actual use, outbox/dead letters, UAMS waits, artifacts, sandbox resources, and error budgets.
- [ ] Run tests, implement telemetry, then rerun.
- [ ] Commit: `feat: add platform telemetry and reliability slos`

### Task 18: Prove recovery, migration rollback, and the branching-repair E2E; remove legacy paths

**Files:**
- Create: `tests/e2e/test_branching_repair_workflow.py`
- Create: `tests/failure/test_worker_recovery.py`
- Create: `tests/failure/test_delivery_and_uams.py`
- Create: `tests/failure/test_artifact_corruption.py`
- Create: `tests/migration/test_release_rollback.py`
- Modify or remove: legacy SQLite, fixed planner, raw-code parser, host-shell, hard-coded generator, and disconnected scheduler modules identified by `rg`
- Create: `docs/operator-guide.md`
- Create: `docs/recovery-guide.md`
- Create: `docs/security-model.md`
- Create: `docs/uams-integration.md`
- Create: `docs/repository-adapters.md`
- Replace: `README.md`

- [ ] Write the deterministic E2E with scripted structured responses: architect creates research, implementation, and test branches; scheduler runs admitted independent tasks in parallel; messages persist; integration intentionally fails; debugger proposes one valid repair mutation; verification passes; exact Git action pauses for approval; approval resumes it; artifacts verify; a UAMS candidate passes the promotion gate.
- [ ] Assert the E2E traverses the real planner, PostgreSQL scheduler, PostgresSaver graph, outbox/Redis transport, tool gateway, constrained sandbox, Git worktrees, artifact store, and UAMS test contract. It must not monkeypatch core services or accept only a final status assertion.
- [ ] Inject worker death during model, checkpoint, tool, outbox, integration, and UAMS promotion boundaries; Redis loss; UAMS unavailability; lease expiry; duplicate events; corrupted artifacts; cancellation of an active sandbox; and unknown side-effect outcomes. Assert the documented deterministic recovery and no duplicate external effects.
- [ ] Populate a prior schema, upgrade, resume a checkpointed workflow, roll back where reversible, and exercise backup restoration where downgrade is unsafe. Start the prior pinned application fixture and prove it can read/resume the compatibility-window state.
- [ ] Search with `rg -n 'sqlite3|MemorySaver|shell\s*=\s*True|```|hard.?coded|FixedPlanner|create_subprocess_shell' --glob '*.py' .`. Remove every production hit and update imports/tests so the new engine is the only authoritative runtime.
- [ ] Write operator, recovery, security, external-UAMS, and adapter-extension runbooks with exact commands and failure states.
- [ ] Run the complete verification gate:

```bash
ruff check .
mypy domain planning persistence messaging knowledge agents workflows tools execution apps observability infrastructure
pytest tests/unit tests/property tests/contract -q
pytest tests/integration tests/security tests/failure tests/migration -q
pytest tests/e2e/test_branching_repair_workflow.py -q
docker compose config --quiet
./scripts/smoke.sh
```

- [ ] Verify `git status --short` contains only intended changes, no credentials, no generated secrets, and no artifact/worktree contents.
- [ ] Commit: `feat: cut over to production agentic engine`

---

## Plan Self-Review Gate

- [ ] Every requirement in Sections 3–18 of the approved design maps to at least one task and an executable test above.
- [ ] The domain/checkpoint two-authority risk has a complete reconciliation-matrix integration suite.
- [ ] The deterministic E2E includes parallel branching, intentional failure, dynamic repair, verification, approval, artifact validation, and UAMS promotion.
- [ ] Artifact metadata/object/hash matching and corruption exclusion are release-gating assertions.
- [ ] PostgreSQL migration acceptance includes tested downgrade or verified backup restoration and prior-version resume.
- [ ] UAMS is external and the sole durable shared memory; Google A2A is absent by explicit scope decision.
- [ ] All model and agent boundaries are structured; all process execution uses argument arrays; all mutable repository work runs in isolated worktrees/containers.
- [ ] No step contains deferred implementation, pseudocode omissions, or unspecified files and commands.
- [ ] The final verification command proves Python and Node.js/TypeScript repository support on the single-machine Docker Compose target.
