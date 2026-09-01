# Repair the observed AutoSWE E2E failures

The approved scope is all defects in `docs/testing/2026-08-31-real-ui-e2e.md`, including the minor status/validation inconsistencies. Preserve existing uncommitted discovery fixes. Do not change historical runs or relax execution safeguards to make a test pass.

## Decisions

- Persist the workspace model configuration in PostgreSQL and snapshot it on each new run. Planning, worker nodes and finalization resolve that snapshot; old runs without a snapshot retain environment defaults. Credentials stay internal and never appear in API responses, audit payloads or logs. Endpoint changes clear the previous provider's key.
- Prefer durable per-run configuration over process-local updates or restarting services on Save. Process-local state caused the observed mismatch; restart-driven updates cannot give concurrent runs a stable configuration.
- Keep the task execution-policy validator authoritative. The planner must construct a policy-compliant plan and repair the actual previous candidate, with task-specific errors, rather than repeatedly generating unrelated candidates. Any deterministic assignment must be restricted to the existing static grants for that task type; never add arbitrary tools or exceed the configured risk ceiling.
- Use `task_id` as the API identifier throughout the browser. Return persisted task description and priority explicitly; do not manufacture IDs. Test with API-shaped fixtures.
- Connecting an existing path must validate an existing repository inside the import root. Importing uploaded files must be explicit, confined and must not overwrite an existing repository. Missing paths do not create starter code.
- Persist useful sanitized planning failure events in the same transaction as terminal state changes. Show friendly lookup/provider errors and honest terminal/connection/auth states.
- Use the configured inference timeout in the connection test. Preserve useful timeout, invalid-URL, provider and malformed-response explanations.
- Replace native cancellation confirmation with an accessible app dialog, and make cancellation discoverable. Verify dialog dismissal through real keyboard actions where available.

## Acceptance

Focused tests reproduce each defect before implementation. Full Python and DOM suites pass after integration. Deploy local services/migrations only when there are no active runs; then use real UI clicks to test task drawers, repository rejection, settings, failures, cancellation and a disposable subtraction run. Independently inspect produced code/tests/artifacts. If a new downstream failure emerges, diagnose and fix it without faking success or modifying recorded state.
