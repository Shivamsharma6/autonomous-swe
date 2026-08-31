# Clean, reliable workspace UI

**Goal:** Make finding, starting, and inspecting runs predictable and easy.

**Design:** Keep the static HTML/CSS/ES-module stack. Use neutral surfaces, a
restrained teal accent, readable typography, and a persistent navigation rail.
The home screen is a searchable run list with static summary counts. New run
has two explicit steps: connect a repository, then describe the work. Run details
show Tasks by default, with Activity, Approvals, and Files in separate tabs.
The dependency graph remains available as an optional task view. Settings and
help are deliberate dialogs; no automatic tour or repeated animation.

**Architecture:** One view controller owns visibility, URL history, request
cancellation, polling, and sockets. Stable keyed run rows retain focus across
refreshes. Errors remain visible with retry actions, and stale responses never
change the current view. Existing API contracts and approval gates remain intact.

**Tech stack:** Native browser modules and dialogs, CSS grid, Node test runner
and jsdom for integration regressions; browser inspection for visual validation.

## Implementation

- [x] Add DOM integration regressions in `tests/web/` for startup with recent
  runs, exclusive sections, duplicate clicks, stale requests, slash typing,
  unchanged polling, model configuration failure, and help reopen.
- [x] Replace competing handlers in `apps/web/app.js`, `js/runsBrowser.js`,
  and `js/palette.js`. Cancel old requests when navigation changes, keep one
  socket for the current task, and refresh only the visible view.
- [x] Replace the accumulated stylesheet and simplify `index.html`. Preserve
  existing controls and IDs; remove false statistics and the fake commit helper.
  Add labelled search, task tabs, visible back navigation, and inline feedback.
- [x] Require a registered repository before launch. Restore real repository
  fields from session state; changing repository fields invalidates that selection.
  Do not quietly change global model configuration from a launch form dropdown.
- [x] Replace broken automatic onboarding with optional, accessible help.
  Remove virtual timeline mode switching (the API already caps events at 500),
  retain payload inspection, and make task selection keyboard accessible.
- [x] Run `npm --prefix tests/web test`, all backend tests, JavaScript syntax and
  diff checks. Exercise desktop and narrow layouts using production assets with
  a disposable local fixture API, then rebuild the real web service and verify it.

## Acceptance

Only one main view is visible. Refreshes do not steal focus, replay animations,
or reopen an old run. No unhandled browser errors on startup, settings, or help.
Keyboard navigation, browser back/forward, loading, empty, signed-out, and API
failure states work. No page-level horizontal overflow at 390 px. No live run is
created, cancelled, or approved during testing. Existing secrets are not exposed.

Verification details and scope are recorded in `docs/ui-cleanup.md`.
