# Ollama discovery in model settings

Model discovery was not run when settings opened or when Local Ollama was selected. The dialog populated its discovered list from saved primary/fallback names, and the Ollama preset supplied hardcoded names. Direct checks of Ollama and the API container both returned ten models, establishing that the local service was reachable.

The dialog now discovers models on open, on Local Ollama selection, and after endpoint/key edits are committed. It distinguishes real discovery results from a configured/custom model, displays loading, empty, and error states inline, and refreshes on reopening. The Ollama preset no longer supplies a model or fallback. Discovery does not save settings or invoke inference.

Abort controllers and request identity checks prevent obsolete discovery/test results from changing a newer dialog. Model edits clear pending or completed connection-test status. Dialog revisions protect new drafts from earlier saves, and queued native close events cannot cancel a reopened dialog's discovery.

The API accepts OpenAI-compatible lists and falls back to Ollama's native list endpoint when the first response is malformed or not a model list. Empty lists stay empty instead of inventing a `default` model. HTTP authentication failures, invalid URLs, and connection failures return actionable discovery errors. Discovery and connection tests use saved provider credentials only for the same normalized endpoint; switching endpoints never forwards an old saved key.

API formats were checked against [Ollama's model-list documentation](https://docs.ollama.com/api/tags) and [OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility).

## Original discovery verification (before E2E repairs)

- `npm --prefix tests/web test`: 34 DOM integration tests, including 11 added discovery/settings regressions.
- `.venv/bin/python -m pytest tests/unit/api/test_model_settings.py -q`: 14 API tests, including 12 added response/credential regressions.
- `.venv/bin/ruff check apps/api/routes.py tests/unit/api/test_model_settings.py`, `node --check apps/web/app.js`, and `git diff --check`.
- Final full Python suite: 367 passed.
- Local API/web image builds and readiness checks.
- Live Ollama discovery checked without saving configuration, starting runs, or issuing inference requests.

The browser tool blocked the sign-in step even for a dummy token on a disposable read-only verification page. Therefore signed-in visual verification is not claimed; interaction coverage comes from the DOM tests and live model discovery is checked separately. The temporary verification server is removed after testing.

A full Python run encountered an intermittent duplicate-key failure in `test_concurrent_failures_increment_one_canonical_delivery_record`. That separate first-insert race was subsequently reproduced deterministically and fixed during the E2E repairs using an atomic PostgreSQL counter upsert. See [the repair report](testing/2026-09-01-ui-repairs.md) for current verification.

## Durable runtime configuration

Saving settings now writes a singleton PostgreSQL configuration row. Launching a run captures its endpoint, model, fallback models, timeout, temperature and private provider key in a run snapshot. Planning, task execution, final review and debugger repair all resolve this snapshot, even after settings change or a service restarts. Existing runs without a snapshot use the environment defaults for compatibility.

The private provider key is not returned in API responses or included in activity or prompts. It is stored in the application database; this change does not add database encryption. Protect database access and backups accordingly.

The connection test uses the draft timeout visible in the dialog. A successful short JSON probe checks connectivity and basic JSON output; it cannot guarantee that the selected model will finish a larger planning or coding request within that timeout. New planning failures include safe diagnostics in Activity.
