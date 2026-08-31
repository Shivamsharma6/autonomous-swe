# Task failure investigation — 2026-08-31

The local deployment had two separate problems: Redis prevented any work from
being dispatched, and earlier runs had already failed in planning or execution.
PostgreSQL task history was retained; no failed or cancelled runs were reset.

## Confirmed causes and fixes

| Failure | Cause | Fix |
| --- | --- | --- |
| Redis, dispatcher, and workers restart repeatedly | Redis's root entrypoint could not traverse its Redis-owned `0700` AOF directory after restart | Restore `DAC_OVERRIDE` for that entrypoint; keep privilege dropping and private file permissions. A disposable-container test verifies data survives restart. |
| Plans cannot execute | Free-form capability names did not match tool capabilities; plans omitted tools required by their subgraphs | Publish and validate an explicit task execution contract. Expand only recognized persisted assignments through static capability grants. Keep tool, role, risk, and path checks. |
| Agents investigate the wrong code | Workers lacked repository inventory and original acceptance context; Python discovery omitted the HTML game | Carry the original goal and criteria, plus a bounded inventory including HTML/JS files. Available task types are alternatives, not a mandatory six-task checklist. |
| Invalid tool calls kill task workers | Argument and policy denials escaped the model loop; searching `.` indexed an empty path tuple | Return denied-call feedback for correction without executing it. Accept the safe relative root. Keep escapes denied. |
| Tasks claim work without doing it | Structured summaries were accepted without tool evidence | Require reads for investigation, writes for mutation, and execution for test stages. Repair invalid output inside the bounded agent loop. Invalidate prior test evidence after writes. |
| Work is stranded after setup failure | Worktree and dependency preparation fell outside failure cleanup | Start heartbeat/graph tracking before preparation, release failed claims, and retain failure diagnostics. Recover worker transport errors with bounded backoff. |
| Tests return stale answers | Sandbox IDs reused command text across distinct calls; Python reused repository bytecode after same-size edits | Bind sandbox execution IDs to persisted tool-call IDs. Use a fresh container-local Python cache. Exact-call replay remains idempotent. |
| Sandbox output is lost or limits count as success | Capture started after the process; final output arrived after the limit check; verification checked only exit code | Attach before starting, check limits after draining, preserve output whitespace, and require both exit zero and `COMPLETED`. |

## Verification and operational scope

Regression coverage includes real Redis restart persistence, real Docker sandbox
bytecode freshness, deterministic output races, tool denial/recovery, missing
repository failures, preparation cleanup, static grants, and unsupported claims.
The scripted end-to-end scenario exercises PostgreSQL, Redis, Docker, repair,
approval, and memory promotion. It does not prove that a live model will solve
every goal or that Python tests establish browser playability.

Local verification commands:

```sh
.venv/bin/python -m pytest tests -q
docker compose ps
docker compose exec -T redis redis-cli ping
```

Verified on 2026-08-31: all **353 tests passed**; changed Python files passed
Ruff, and `git diff --check` passed. The platform image built successfully. The
updated API, dispatcher, workers, and sandbox manager were restarted and became
healthy. `/health/ready` reported PostgreSQL, Redis, checkpoints, sandbox, model,
and UAMS ready; Redis answered `PONG`. No historical run was automatically retried,
and no new live-model goal was submitted as part of verification.

Historical planning calls also recorded provider timeouts. Those external
availability failures are distinct from the reproduced code defects. The fixes
do not bypass provider limits, sandbox restrictions, or release approval.
