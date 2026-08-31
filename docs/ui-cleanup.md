# UI cleanup — 2026-08-31

The dashboard now has three distinct views: All runs, New run, and run details.
It keeps the existing static HTML/CSS/JavaScript stack and API contracts.

## What changed

- Replaced the layered dark styles, animated cards, ticker, and marketing banner
  with neutral surfaces, consistent spacing, a navigation rail, and a compact run
  list. Status filters include cancelled runs. Search has an explicit clear state.
- New run is a two-step form with a real repository connection before launch.
  Removed hardcoded machine paths, sample projects, and fake commit hashes.
  Editing repository fields invalidates the previous selection. Late folder
  imports cannot overwrite a newer repository choice.
- Run details default to a readable task list. The graph remains optional.
  Activity, approvals, and files have separate accessible tabs. Tasks and file
  previews support keyboard activation. Approval decisions use a native dialog.
- Polling retains focused run/task elements, ignores stale responses, and stops
  when leaving a view. One socket follows the active task. Browser history works
  without discarding an unfinished goal. Hidden browser tabs avoid polling work.
- Fixed startup failures caused by missing `DEFAULT_MODELS`, mismatched recent-run
  IDs, and the automatic tour. Help is now optional and uses a native dialog.
  Shortcuts no longer intercept slashes in text fields or open over other dialogs.
- Activity uses real `event_id` values to deduplicate events and preserve expanded
  payloads. Recent socket events survive refreshes even when the API returns an
  older 500-event window. The UI retains at most 500 events.
- Signed-out, empty, loading, and unavailable states show clear next actions.
  Settings responses cannot restore model state after sign-out. File previews
  cancel outdated requests. Choosing a model in the launch form no longer
  silently changes global settings.
- Switching provider endpoints with a blank key now clears the previous
  provider's key. A blank key retains it only for the same normalized endpoint.
- Static assets now explicitly require cache revalidation. Deployment inspection
  caught a browser still loading the old UI despite the server serving new files.
  Entry assets and module imports use a release version in their URLs to migrate
  existing browser caches; subsequent responses carry `Cache-Control: no-cache`.

## Verification

- 23 DOM integration regressions pass against the actual application modules.
- All 355 Python tests pass, including two provider-key regression cases.
- Changed Python files pass Ruff; JavaScript syntax and diff whitespace checks pass.
- Both platform and web images build. Updated API and web services are deployed
  locally; no real run, approval, provider configuration, or repository import was
  submitted through the browser during testing.
- Browser inspection covers desktop and 390 px layouts, connection, new-run
  forms, tabs, settings, and Back/Forward navigation using a disposable fixture
  API. No page-level horizontal overflow was observed at 390 px.

Run the UI suite:

```sh
npm --prefix tests/web ci --ignore-scripts
npm --prefix tests/web test
```

For a safe visual preview with synthetic data, run
`node tests/web/preview.mjs` and open `http://127.0.0.1:4173`. Any 32-character
test token works in this fixture. It never forwards to the actual control plane.
The normal dashboard is at `http://127.0.0.1:3000`.

Model provider availability and live agent quality are outside these UI checks.
The activity endpoint still limits persisted results to its first 500 events;
the UI preserves subsequently streamed events only for the current loaded run.
