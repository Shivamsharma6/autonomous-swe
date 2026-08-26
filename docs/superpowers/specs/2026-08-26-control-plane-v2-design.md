# AutoSWE Control Plane v2 — "Mission Control" UI/UX Design

Date: 2026-08-26
Status: Approved (user delegated to recommended options)

## Context

The control plane is a zero-build static SPA served by nginx from `apps/web/`
(`index.html`, `app.js` 1548 lines, `styles.css` 2025 lines) against the FastAPI
control plane on loopback :8080. A prior pass ("Open Code Codex aesthetic")
established a strong dark visual system (emerald/cyan accents, JetBrains Mono,
Plus Jakarta Sans). Recent backend work added model configuration/probe/test
endpoints, folder onboarding, run cancellation, and richer durable state.

Weaknesses today:

- Single-run tunnel vision: no way to browse or compare runs; recovery requires
  pasting a run ID.
- Task inspector is thin: no agent summaries or handoff messages visible
  (operators reconstruct context by downloading artifacts).
- `app.js` is a 1,500-line IIFE monolith; adding features raises regression risk.
- Feedback patterns are basic (single toast, full re-fetch refreshes).
- Timeline lacks filtering ergonomics and payload inspection.
- Accessibility and motion polish are incomplete.

## Recommended decisions (user-delegated)

1. **Stack: keep zero-build vanilla ES modules.** No framework, no bundler —
   nginx keeps serving static files unchanged. `app.js` is split into focused
   modules under `apps/web/js/` (api, ws, palette, dag, timeline, views, util)
   loaded via `<script type="module">`. Rationale: deployment simplicity is a
   product feature of this repo; "state of the art" comes from UX quality and
   information architecture, not a framework badge.
2. **Visual system: evolve, don't replace.** Keep the established dark Codex
   tokens; refine spacing/elevation/motion consistency. No light theme (YAGNI),
   `prefers-reduced-motion` respected.
3. **Information architecture: two surfaces.**
   - *Runs Browser* (new landing view): all runs across projects, status filter
     chips, live status pills, relative timestamps, cost/token columns, click
     through to dashboard. Requires new API `GET /api/v1/runs`.
   - *Run Dashboard* (existing, upgraded): HUD, live DAG, approvals, artifacts,
     timeline stay; task drawer gains real intelligence.
4. **Task drawer intelligence:** new API
   `GET /projects/{pid}/tasks/{tid}/messages` returning the newest handoff
   summaries/messages; drawer renders an "Agent reasoning" stream so operators
   see what agents concluded without artifact downloads.
5. **Command palette (⌘K / `/`):** fuzzy search across recent runs, projects,
   and actions (launch, cancel, copy IDs, open model studio, jump timeline
   filters). Keyboard-first operator UX.
6. **Feedback systems:** stacked non-blocking toasts with severity; skeleton
   shimmer loaders; deliberate empty states; optimistic status pill updates;
   WebSocket auto-reconnect with backoff and visible connection beacon.
7. **Timeline v2:** type/severity filter chips persisted per session,
   hover-pauses autoscroll, expandable JSON payloads with copy button,
   monotonic virtualized-ish rendering (chunked appends, capped buffer).

## Backend additions (small, aligned with existing contracts)

- `GET /api/v1/runs?project_id&status&limit&offset` →
  `tuple[RunSummaryResponse, ...]`: id, project name, goal excerpt, state,
  plan revision, task counts by state, token/cost totals, created/updated.
- `GET /api/v1/projects/{project_id}/tasks/{task_id}/messages?limit=` →
  newest `MessageResponse[]` (id, kind, sender, recipient, summary, created_at).

Both read-only, admin-scoped by the existing router dependency, and backed by
the existing repositories (no schema changes).

## Non-goals

Light theme, i18n, third-party chart/DAG libraries, framework migration,
multi-user RBAC UI, mobile-first layouts (responsive down to ~1024px only).

## Testing

- Backend: contract tests for both new endpoints (auth, filtering, empty
  results); reuse existing integration fixtures.
- Frontend: `node --check` syntax gate on every module; manual smoke checklist
  documented in the PR description (no JS test runner in repo — consistent with
  current practice).
