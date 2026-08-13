# README Architecture Reference Design

## Purpose

Turn the root README into the canonical, self-contained architectural reference for AutoSWE.
It must let an engineer or reviewer understand why the platform exists, which component owns each
decision, how one run moves through the system, what survives failure, and which claims are backed
by acceptance tests without requiring an initial source-code tour.

## Chosen approach

Use a layered long-form README. Start with the system promise and non-goals, introduce the runtime
topology and authority boundaries, then progressively deepen into planning, scheduling, LangGraph,
agent execution, tools, sandboxing, memory, persistence, release gates, observability, recovery,
security, deployment, API use, extension points, and verification. Keep the focused documents in
`docs/` as operator drill-downs rather than duplicating every procedure in the README.

This is preferred over a short landing page because AutoSWE is a showcase architecture: the
decisions and failure semantics are part of the product. It is preferred over splitting an
architecture book across many new documents because a first-time reviewer should be able to assess
the design from one entry point.

## Documentation invariants

- Describe only behavior present in the production path and verified by code or tests.
- Name PostgreSQL as domain authority, PostgresSaver as execution-cursor authority, Redis as
  disposable delivery transport, Git as source authority, artifacts as evidence, and UAMS as the
  only cross-run knowledge store.
- Keep task scheduling state separate from LangGraph execution state.
- Describe at-least-once delivery and replay-safe/idempotent side effects; never imply exactly-once
  distributed execution.
- Show all six task-specific subgraphs and the bounded dynamic-repair path.
- Explain the final validation sink, artifact rehashing, exact-call approval, commit, and UAMS
  promotion gates.
- Make single-machine and Docker-daemon trust limitations explicit.
- State that A2A, distributed orchestration, host-shell execution, SQLite fallback, autonomous
  deployment, and unaudited arbitrary tools are out of scope.
- Keep examples argv-structured and keep secrets as environment-variable names only.

## Information architecture

1. Project intent, capabilities, design principles, and non-goals.
2. Architecture diagrams and component ownership table.
3. End-to-end run lifecycle and typed task workflows.
4. Separate scheduler and LangGraph state machines.
5. Planner validation, scheduler admission, agent runtime, governed tools, and sandbox execution.
6. Durability, messaging, reconciliation, artifacts, Git finalization, and UAMS memory promotion.
7. Security boundaries, observability, SLOs, deployment topology, operations, and failure recovery.
8. API surface, repository layout, extension contracts, verification matrix, and limitations.

## Acceptance criteria

- Every major production component is named with its responsibility and authority boundary.
- The README includes exact default concurrency and plan-mutation limits and all initial SLOs.
- Quick-start and validation commands match the checked-in scripts and Compose files.
- All relative Markdown links resolve to tracked files.
- No secret, placeholder, legacy path, or unsupported production claim is introduced.
- The full test suite and both Compose configurations still pass after the documentation commit and
  after local merge to `main`.
