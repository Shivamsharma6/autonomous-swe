# README Architecture Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the root README the canonical, accurate architectural reference for the production AutoSWE platform and merge the documented platform into local `main`.

**Architecture:** Replace the short overview with a progressive-disclosure reference grounded in current contracts, workflows, Compose topology, operator guides, and acceptance tests. Keep specialized documents linked for detailed procedures, verify documentation mechanically, commit on the feature branch, merge locally, and rerun the complete acceptance gate on `main`.

**Tech Stack:** Markdown, Mermaid, Docker Compose, Python, LangGraph, PostgreSQL, Redis Streams, UAMS, Prometheus, OpenTelemetry, Grafana, Git.

---

### Task 1: Build the canonical architecture README

**Files:**
- Modify: `README.md`
- Reference: `domain/enums.py`
- Reference: `workflows/task_subgraphs.py`
- Reference: `execution/scheduler/reconciliation.py`
- Reference: `observability/slo.py`
- Reference: `docker-compose.yml`
- Reference: `docs/operator-guide.md`
- Reference: `docs/security-model.md`
- Reference: `docs/recovery-guide.md`
- Reference: `docs/uams-integration.md`

- [x] **Step 1: Replace the README with the approved layered architecture reference**

Cover the platform contract, component authority table, runtime and execution diagrams, typed
subgraphs, separate state machines, concurrency and mutation limits, durability semantics, UAMS,
tool/sandbox security, release gates, telemetry/SLOs, Compose operations, API, recovery,
extensions, limitations, and verification.

- [x] **Step 2: Scan for placeholders, legacy architecture, and unsafe command examples**

Run:

```bash
rg -n 'TODO|TBD|SQLite|shell=True|subprocess.*shell|games_demo|WorkflowOrchestrator' README.md
```

Expected: no unintentional legacy or placeholder references. An explicit statement that SQLite and
host-shell execution are out of scope is allowed and must be manually reviewed.

- [x] **Step 3: Verify every local Markdown link resolves**

Run the repository documentation-link test command introduced by the existing acceptance suite:

```bash
.venv/bin/pytest tests/compose -q
```

Expected: all Compose/documentation assertions pass.

### Task 2: Verify and commit the documentation

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/specs/2026-08-13-readme-architecture-reference-design.md`
- Create: `docs/superpowers/plans/2026-08-13-readme-architecture-reference.md`

- [x] **Step 1: Run static and repository acceptance checks**

```bash
.venv/bin/ruff check .
.venv/bin/mypy domain planning persistence messaging knowledge agents workflows tools execution apps observability infrastructure
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.observability.yml config --quiet
git diff --check
```

Expected: every command exits zero.

- [x] **Step 2: Commit the documentation**

```bash
git add README.md docs/superpowers/specs/2026-08-13-readme-architecture-reference-design.md docs/superpowers/plans/2026-08-13-readme-architecture-reference.md
git commit -m "docs: add architecture deep dive"
```

Expected: a new commit on `codex/production-agentic-platform`.

### Task 3: Merge and verify local main

**Files:**
- Verify: complete repository

- [ ] **Step 1: Merge the feature branch from the clean main worktree**

```bash
git merge codex/production-agentic-platform
```

Expected: a successful fast-forward or merge with no conflicts.

- [ ] **Step 2: Run the complete test suite on merged main**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Verify final state and clean up the owned worktree**

```bash
git status --short
git worktree remove /Users/shivamsharma/projects/autonomous-swe/.worktrees/production-agentic-platform
git worktree prune
git branch -d codex/production-agentic-platform
```

Expected: `main` is clean, the merged feature worktree is removed, and the merged branch is
deleted.
