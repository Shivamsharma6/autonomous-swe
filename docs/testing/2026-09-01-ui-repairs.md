# AutoSWE repairs and real UI verification

Verification started 31 August 2026 UTC and continued into 1 September IST. The original failure evidence remains in `2026-08-31-real-ui-e2e.md`.

**Status: repairs implemented and automated verification passed. Final successful real UI execution is not yet established; restoring the test configuration requires explicit approval after a browser safety rejection.**

## Implemented repairs

- Planner grants now include the authoritative minimum tools for each supported task type, without accepting unknown tools or raising risk ceilings. Repairs receive the previous candidate and task-specific validation issues.
- Terminal planning failures emit sanitized, transactional `run.planning_failed` activity. Cancellation and ownership fences prevent stale planning results from changing a terminal run.
- Workspace model configuration is persisted in PostgreSQL. New runs store an immutable launch snapshot used by planning, workers, final review, and debugger repair. Historical runs without snapshots retain environment defaults.
- Task lists, graphs, details, live updates, and event filtering use canonical `task_id`. The API also supplies description and priority.
- Connecting requires an existing Git repository and committed branch inside the import root. Uploads cannot overwrite existing folders or escape their destination; import failures clean up only newly created data.
- Connection tests use the edited inference timeout and explain timeout, provider, and malformed-response failures.
- Run lookup validates UUIDs, invalid tokens do not report success, repository edits clear stale connected feedback, and terminal runs do not display ongoing planning.
- Cancellation uses an accessible in-app dialog. Dialog Escape/focus handling and stale asynchronous responses are guarded.
- Provider error bodies no longer flow into gateway exceptions, and SQLAlchemy hides bind parameters in diagnostic output.
- An ownership watcher now interrupts an active architect request or retry backoff within its 250 ms polling interval and drains both child tasks on every exit.
- Concurrent delivery failures use an atomic PostgreSQL counter upsert instead of trying to lock a row that does not exist yet.
- Model retries and fallbacks now receive distinct accounting ordinals, so a timeout cannot suppress a later successful response's token usage. Logical turn budgets and recording idempotency remain intact; historical missing usage is not fabricated or backfilled.
- Workers execute up to the configured task capacity concurrently and start each task's heartbeat promptly. Free slots refill independently; stop drains running work and forced cancellation joins all children. Lease ownership checks and lease duration remain unchanged.
- Test tool arguments describe and validate the operation/target relationship before adapter or sandbox work. File tool contracts preserve exact content, including whitespace and empty files, without relaxing command policy.
- Worker nodes use the shared 40,000-token agent policy instead of a separate 20,000-token cap that could cut off a normal multi-file tool exchange. Existing $2, twelve-turn, and 900-second limits remain unchanged. A regression also verifies that exceeding the token budget still fails.
- Task-node failures now publish sanitized `task.node_failed` audit/outbox activity transactionally, including task ID, node, error classification and recovery guidance. Repeated failure recording does not duplicate the event.
- Planner and execution prompts clarify dependency ordering for tests of newly implemented behavior and the scope of an assigned task/node.

Provider credentials remain private database configuration, not encrypted by this change. Public API responses, activity events, and model prompts do not include them.

## Automated verification and final deployment

- Initial full Python suite: **426 passed**; intermediate suite: **434 passed**. **Final full Python suite: 467 passed in 51.63 seconds.**
- Final browser DOM suite: **65 passed**.
- Ruff passed; mypy passed across **57 source files**; JavaScript syntax and diff checks passed.
- The deterministic delivery-race regression passed **10 consecutive isolated repeats**; all **9 messaging integration tests** passed.
- All **52 planner integration tests** passed, including cancellation during real HTTP waits and retry backoff, model-capacity release, and next-run progression.
- Worker coverage includes a real disposable PostgreSQL/Redis scenario where three tasks start concurrently, renew leases beyond their original expiry, finish, release leases and acknowledge their envelopes.
- A scripted production workflow test passed, but is not represented as a successful live-model UI run.
- Migration `0012` applied successfully, with no remaining schema drift.
- Final local platform and web images rebuilt and deployed. All eight Compose services are healthy; API readiness reports all six dependencies available. The model readiness check verifies configuration availability, not successful inference with the saved model.
- Review found no outstanding major issues in the initial repairs. The worker attempt identity changed, so the initial rollout was performed with **zero active runs**. Future upgrades involving existing active attempts must preserve their identity or drain them first.

## Real interactions verified

All product mutations below used the in-app browser controls. Database queries, logs, and fixture checks only corroborated results. Historical runs were inspected, not modified.

| Check | Observed result |
| --- | --- |
| Invalid token then valid authentication | Rejection stays in dialog, without a false success message; valid token loads workspace |
| Invalid run lookup | Clear UUID guidance; valid lookup recovers |
| Lookup warning on navigation | Invalid lookup followed by New run clears the irrelevant warning in the final deployed build |
| Ollama discovery | Ten actual models listed |
| Configured timeout | Ten-second Nemotron probe ends at **10,017.9 ms**, with actionable timeout guidance |
| Real Gemma test | Valid JSON completion in **4,276.3 ms** |
| Saved configuration | Survives restarting the API service |
| Missing repository | Rejected; `ui-repair-path-does-not-exist` is not created |
| Existing repository | Correct baseline and path connected; source remains unchanged |
| Repository edits | Connection invalidated and obsolete success message removed |
| Task list and details | Actual list click opens canonical ID, description, priority, agent handoffs, and task-scoped activity |
| Graph | Six historical tasks display in four correct dependency stages; no empty root stage |
| Keyboard dialogs | Escape closes task details, quick guide, settings, and cancellation dialog |
| Cancellation | In-app confirmation works; cancelled run displays No plan and `run.cancelled` activity |
| Model runtime selection | Database records actual Gemma invocation after workspace default changed back to Nemotron |
| Previously failing planning | Nemotron eventually produces a valid four-task plan and workers execute real tools |
| Visible planning failure | Deliberately missing model produces `run.planning_failed`; expanding it shows safe provider-error classification and recovery guidance |

## Live test runs

Fixture: `runtime/imports/ui-e2e-calculator-20260831`, baseline `9b619d46c7d3eb54e4f1c48f0e560ca4465104ee`. Original fixture unittest passed from its repository directory. No dependencies, remote Git operations, or external deployments are authorized by these test goals.

| Run | Purpose | Current result |
| --- | --- | --- |
| `63731e5b-7866-45ef-bd1c-92d549dde750` | Gemma selection and subtraction implementation | Actual Gemma request exceeded 120 seconds; cancelled through UI |
| `12bf768c-e2d7-4b81-be69-152d606a111f` | In-app cancellation | Cancelled, zero tasks |
| `0e6f13a9-247f-4320-b167-027ab8e1399a` | Complete subtraction workflow using Nemotron | Planning succeeds after a timeout/retry; execution fails, revealing further defects subsequently repaired |
| `c8f34ee9-7c92-4c03-b1e2-2120d6579334` | Deliberately nonexistent model for visible failure diagnostics | Expected failure; safe cause verified by expanding Activity |

Live testing exposed an additional cancellation gap: an in-flight architect continued internal retries after its run and stage were cancelled, delaying queued work. The regression-backed repair is now deployed. Independent review confirmed scoped task cleanup and retained transactional fences.

The Nemotron run reached actual execution, read `src/calculator.py` and `tests/test_calculator.py`, and wrote the correct subtraction function and requested test cases in its isolated task worktree. Its `run_tests` call supplied `operation: full_test` together with `target: tests`, which the advertised tool model accepted but the command layer rejected. Recovery then hit the 20,000-token node cap. Other envelopes waited behind the serial worker and lost their initial leases. These observations led to the tool-contract, budget, concurrent-worker, accounting and task-diagnostics repairs listed above. This failed run was not edited into a success, retried by direct API calls, or counted as a passing final workflow.

At the final deployment checkpoint, all fifteen retained runs were terminal: nine failed and six cancelled. No original historical runs were changed. The new fixes still need a fresh live-model UI run after the saved model is restored.

The browser safety check blocked restoring the original model after the deliberately invalid-model test. Explicit approval was requested; until restoration is confirmed, the saved workspace model must not be treated as usable for new work. Existing runs use their launch snapshots.

## Evidence and remaining limits

- `evidence/2026-08-31-ui-repairs/01-model-timeout.jpg`
- `evidence/2026-08-31-ui-repairs/02-task-graph.jpg`
- `evidence/2026-08-31-ui-repairs/03-planning-failure-activity.jpg`
- `evidence/2026-08-31-ui-repairs/run-diagnostics.json` (selected fields only; no provider credentials)
- Native folder selection remains outside available desktop-control permissions. API import tests cover destination safety and exact file preservation, but do not prove operation of the native picker.
- No full-product pass is claimed while successful execution, approvals, and delivery remain unverified.
