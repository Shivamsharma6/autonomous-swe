# Recovery guide

PostgreSQL domain rows are canonical. LangGraph checkpoints are independently durable execution
cursors. Redis Streams are transport and may be rebuilt from PostgreSQL outbox records. UAMS is
external and never has a local fallback.

## First response

```bash
docker compose ps
curl --silent --show-error http://127.0.0.1:8080/health/ready
docker compose logs --since 30m api dispatcher workers sandbox-manager postgres redis
./scripts/reconcile.sh
```

Do not manually update task, graph, lease, approval, artifact, or memory rows. Reconciliation is
idempotent and writes an immutable audit event for each logical correction.

## Domain/checkpoint divergence

The deterministic recovery rules are:

| PostgreSQL task | Graph checkpoint | Result |
|---|---|---|
| `RUNNING`, expired lease | resumable | release reservation, return task to `READY`, start a fresh checkpoint chain |
| `RUNNING` | `COMPLETED` | finalize the domain task as `COMPLETED` |
| `RUNNING` | missing | return task to `READY` and start from replay-safe node boundaries |
| terminal task | graph disagrees | quarantine; the disagreement is reported rather than resolved silently |
| terminal task | nonterminal graph | domain terminal state wins; cancel graph metadata |
| identity/baseline mismatch | any | quarantine as `BLOCKED` / `NEEDS_RECONCILIATION` |
| `FAILED` task | `COMPLETED` graph | quarantine; never infer success |

Run `./scripts/reconcile.sh` twice when validating an incident. The second execution should report
only no-op outcomes.

### A re-dispatch re-executes; it does not resume

Each lease attempt gets its own LangGraph `thread_id` and its own node idempotency keys, so a
task that is returned to `READY` runs again from its first node rather than continuing from its
checkpoint. That is deliberate: a shared thread across attempts would let one attempt's state be
appended onto a divergent chain.

It has a cost that operators must plan for. A retried task is paid for again in full, including
every model call it already made. Two ceilings bound this, and both are enforced at admission
before any work is spent:

- `AUTOSWE_MAX_TASK_ATTEMPTS` (default 3) lease attempts per task. A task that exhausts its
  attempts is moved to `FAILED`, its lease and reservations are released, and the run can finish.
- `AUTOSWE_MAX_TOTAL_BUDGET_USD` (default 25.0) accumulated model cost per run, summed from
  `model_calls`. A task that would start beyond the ceiling is refused the same way.

If a task is being retried repeatedly, read the task's `task_attempts` rows before raising a
limit: a repeating lease expiry is an environment fault (a killed worker, an unreachable sandbox),
not a task that deserves more attempts.

## Worker or sandbox failure

- A worker crash leaves a lease and a synchronous PostgresSaver checkpoint. After the lease
  expires, reconciliation makes the task eligible for replay.
- Every node has a stable idempotency boundary. Persisted model/tool/node results are returned on
  replay. Unknown external side effects become `NEEDS_RECONCILIATION`; they are not retried.
- Sandbox container IDs are persisted before start. Sandbox-manager startup kills orphaned
  containers and records an interrupted outcome.
- Cancellation is cooperative, not immediate. It stops the next workflow node from starting and
  the worker notices within one heartbeat interval (10s). A sandbox container that is already
  running continues until it reaches its own wall-clock, CPU, or output limit: nothing currently
  signals the sandbox manager to kill it mid-execution. If a cancelled run leaves containers
  consuming resources, restart `sandbox-manager`, whose startup sweep kills orphans.
- Initial worker-failure recovery objective: 95% within 60 seconds when a durable checkpoint and
  retry budget exist.

## Redis or delivery failure

PostgreSQL outbox rows and consumer receipts prevent loss and duplicate effects. After Redis loss:

```bash
docker compose restart redis dispatcher workers
docker compose exec -T redis redis-cli XINFO STREAM task-dispatch
```

The publisher republishes canonical outstanding events. A repeated stable event ID is expected;
consumer receipts make the effect once-only. The eighth failed delivery is retained as a dead
letter. Inspect and replay it through `/api/v1/dead-letters` only after fixing the cause.

## UAMS failure

UAMS failures move execution to `WAITING_FOR_MEMORY`; no SQLite or in-process fallback is used.
Restore the external service, confirm `GET /ready`, and leave the dispatcher running. Promotion
reuses the deterministic memory UUID. Completion requires UAMS to report the current revision as
active, indexed, and searchable. Repeated retries do not reset the original UAMS wait clock.

## Artifact corruption

Every download and final review rehashes the storage object. A mismatch marks the metadata
`CORRUPT`, moves the bytes to quarantine, emits audit/outbox events, and removes it from valid
evidence. Do not copy the object back. Recreate evidence through a new task or restore the whole
database/artifact pair from the same backup epoch.

## Database migration or host loss

For the latest reversible release:

```bash
./scripts/backup.sh
./scripts/migrate.sh
# If validation fails and the release notes permit downgrade:
docker compose run --rm migrations alembic downgrade -1
./scripts/reconcile.sh
```

When a downgrade would discard data, restore instead:

```bash
./scripts/restore.sh --confirm /absolute/path/to/verified.dump
curl --fail http://127.0.0.1:8080/health/ready
./scripts/reconcile.sh
```

The acceptance suite proves a checkpoint created on schema `0009` resumes after `0010`, then
survives reversible rollback. The smoke test verifies backup catalog/hash validation and actual
restoration of a database marker.
