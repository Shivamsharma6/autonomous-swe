import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, cp, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';
import { fixture, runs, tasks, events } from './fixtures.mjs';

const source = new URL('../../apps/web/', import.meta.url);
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
async function setup(t, { recent = [], fetcher, token = true } = {}) {
  const dir = await mkdtemp(`${tmpdir()}/autoswe-ui-`);
  await cp(source, dir, { recursive: true });
  await writeFile(`${dir}/package.json`, '{"type":"module"}');
  const dom = new JSDOM(await readFile(new URL('index.html', source), 'utf8'), { url: 'http://localhost/', pretendToBeVisual: true });
  const { window } = dom;
  const timers = new Map();
  let nextTimer = 0;
  window.setTimeout = (fn, ms) => {
    const id = ++nextTimer;
    if (!ms) queueMicrotask(fn); // Let native history traversal dispatch popstate.
    else timers.set(id, { fn, ms });
    return id;
  };
  window.clearTimeout = id => timers.delete(id);
  window.setInterval = () => 0;
  window.scrollTo = () => {};
  window.requestAnimationFrame = cb => { cb(); return 0; };
  window.cancelAnimationFrame = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
  window.ResizeObserver = class { observe() {} disconnect() {} };
  if (token) window.sessionStorage.setItem('autoswe.adminToken', 'test-token-for-local-fixtures-only');
  window.localStorage.setItem('autoswe.recentRuns', JSON.stringify(recent));
  window.localStorage.setItem('autoswe.tourSeen.v2', '1');
  const calls = [];
  const sockets = [];
  const fetch = async (path, options = {}) => {
    calls.push({ path, options });
    const custom = await fetcher?.(path, options);
    return custom ?? new Response(JSON.stringify(fixture(path)), { status: 200 });
  };
  Object.assign(globalThis, { window, document: window.document, sessionStorage: window.sessionStorage, localStorage: window.localStorage, fetch, ResizeObserver: window.ResizeObserver, requestAnimationFrame: cb => { cb(); return 0; }, cancelAnimationFrame() {}, WebSocket: class { constructor(url) { this.url = url; sockets.push(this); } close() { this.onclose?.(); } } });
  t.after(async () => { await flush(); dom.window.close(); await rm(dir, { recursive: true, force: true }); });
  await import(pathToFileURL(`${dir}/app.js`));
  await flush();
  return { window, document: window.document, calls, sockets, timers, click: selector => window.document.querySelector(selector).click(), flush };
}

test('startup with saved recent runs keeps navigation usable', async t => {
  const ui = await setup(t, { recent: [{ id: runs[0].run_id, goal: runs[0].goal, time: 1 }] });
  ui.click('[data-nav="new"]');
  assert.equal(ui.document.querySelector('#onboardingSection').classList.contains('hidden'), false);
  assert.equal(ui.document.querySelector('#runsBrowserSection').classList.contains('hidden'), true);
  ui.click('[data-nav="runs"]');
  assert.equal(ui.document.querySelector('#onboardingSection').classList.contains('hidden'), true);
});

test('one run click makes one detail request; task summary is readable', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]');
  await flush();
  assert.equal(ui.calls.filter(c => c.path === `/api/v1/runs/${runs[0].run_id}`).length, 1);
  assert.match(ui.document.querySelector('#taskSummary').textContent, /2.*5/);
  assert.equal(ui.document.querySelector('#dashboard').classList.contains('hidden'), false);
});

test('leaving a pending run prevents its late response from reopening the detail view', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === `/api/v1/runs/${runs[0].run_id}` ? pending : undefined });
  ui.click('[data-run-id]');
  ui.click('[data-nav="new"]');
  release(new Response(JSON.stringify(runs[0])));
  await flush();
  assert.equal(ui.document.querySelector('#dashboard').classList.contains('hidden'), true);
  assert.equal(ui.document.querySelector('#onboardingSection').classList.contains('hidden'), false);
});

test('slash and command shortcuts do not interrupt editing in a form or dialog', async t => {
  const ui = await setup(t);
  ui.click('[data-nav="new"]');
  const input = ui.document.querySelector('#sourcePath');
  input.focus();
  const slash = new ui.window.KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true });
  input.dispatchEvent(slash);
  assert.equal(slash.defaultPrevented, false);
  assert.equal(ui.document.activeElement, input);
});

test('refresh retains a focused unchanged run row', async t => {
  const ui = await setup(t);
  const row = ui.document.querySelector('[data-run-id]');
  row.focus();
  ui.click('#runsRefresh');
  await flush();
  assert.equal(row.isConnected, true);
  assert.equal(ui.document.activeElement, row);
});

test('model errors leave a usable settings dialog and no invented model', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/config' ? new Response('{"detail":"Unavailable"}', { status: 503 }) : undefined });
  ui.click('#modelStudioBtn');
  assert.equal(ui.document.querySelector('#modelStudioDialog').open, true);
  assert.doesNotMatch(ui.document.querySelector('#launchpadModelSelect').textContent, /gemma4/);
});

test('help can open, close, and reopen without overlay errors', async t => {
  const ui = await setup(t);
  ui.click('#tourHelpBtn');
  assert.ok(ui.document.querySelector('dialog[open]'));
  ui.document.querySelector('dialog[open]').close();
  ui.click('#tourHelpBtn');
  assert.ok(ui.document.querySelector('dialog[open]'));
});

test('refresh keeps one socket and task button for the same running task', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush();
  const button = ui.document.querySelector('.task-list-row');
  button.focus();
  assert.equal(ui.sockets.length, 1);
  ui.click('#refreshRun'); await flush();
  assert.equal(ui.sockets.length, 1);
  assert.equal(button.isConnected, true);
  assert.equal(ui.document.activeElement, button);
});

test('switching between runs cannot apply a delayed prior response', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === `/api/v1/runs/${runs[0].run_id}` ? pending : undefined });
  ui.click('[data-run-id]');
  const lookup = ui.document.querySelector('#runLookup');
  lookup.value = runs[1].run_id;
  ui.document.querySelector('#lookupForm').dispatchEvent(new ui.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush();
  release(new Response(JSON.stringify(runs[0]))); await flush();
  assert.equal(ui.document.querySelector('#runGoalTitle').textContent, runs[1].goal);
});

test('activity filters never duplicate stale virtual rows and preserve expanded payloads', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush();
  ui.click('[data-panel="activity"]');
  assert.equal(ui.document.querySelectorAll('#eventList li').length, 55);
  const detail = ui.document.querySelector('#eventList details');
  detail.open = true;
  ui.click('#refreshRun'); await flush();
  assert.equal(detail.open, true);
  ui.click('#timelineFilters [data-filter="TOOL"]');
  assert.equal(ui.document.querySelectorAll('#eventList li').length, 27);
  ui.click('#timelineFilters [data-filter="ALL"]');
  assert.equal(ui.document.querySelectorAll('#eventList li').length, 55);
  assert.equal(ui.document.querySelectorAll('.virtual-viewport').length, 0);
});

test('signed-out startup offers connection without opening a blocking dialog', async t => {
  const ui = await setup(t, { token: false });
  assert.equal(ui.document.querySelectorAll('dialog[open]').length, 0);
  assert.match(ui.document.querySelector('#runsGrid').textContent, /Connect workspace/);
  assert.equal(ui.calls.filter(c => c.path.startsWith('/api/')).length, 0);
});

test('failed list loads have a visible retry action instead of disappearing placeholders', async t => {
  let failing = true;
  const ui = await setup(t, { fetcher: path => path.startsWith('/api/v1/runs?') && failing ? new Response('{"detail":"Offline"}', { status: 503 }) : undefined });
  assert.match(ui.document.querySelector('#runsFeedback').textContent, /Offline/);
  failing = false;
  ui.click('[data-action="retry"]'); await flush();
  assert.equal(ui.document.querySelectorAll('[data-run-id]').length, 4);
  assert.equal(ui.document.querySelector('#runsFeedback').classList.contains('hidden'), true);
});

test('repository edits invalidate registration and prevent sending a run to the previous repository', async t => {
  const ui = await setup(t, { fetcher: path => path.endsWith('/onboard') ? new Response(JSON.stringify({ project_id: runs[0].project_id, repository_id: runs[0].repository_id, name: 'Checkout', source_path: 'checkout', default_branch: 'main', baseline_commit: 'a'.repeat(40) })) : undefined });
  ui.click('[data-nav="new"]');
  ui.document.querySelector('#projectName').value = 'Checkout';
  ui.document.querySelector('#sourcePath').value = 'checkout';
  ui.document.querySelector('#projectForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  await flush();
  assert.equal(ui.document.querySelector('#startRun').disabled, false);
  const sourcePath = ui.document.querySelector('#sourcePath');
  sourcePath.value = 'other'; sourcePath.dispatchEvent(new ui.window.Event('input'));
  assert.equal(ui.document.querySelector('#startRun').disabled, true);
  ui.document.querySelector('#runForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  await flush();
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/runs' && c.options.method === 'POST').length, 0);
});

test('command palette uses current recent-run IDs and does not reopen over a dialog', async t => {
  const ui = await setup(t, { recent: [{ id: runs[0].run_id, goal: runs[0].goal, time: 1 }] });
  ui.click('#openCommandPalette');
  assert.match(ui.document.querySelector('#paletteList').textContent, /Add resilient webhook/);
  ui.document.querySelector('#paletteDialog').close();
  ui.click('#tourHelpBtn');
  ui.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
  assert.equal(ui.document.querySelector('#paletteDialog').open, false);
});

test('real event_id replay is deduplicated and recent streamed events survive refresh', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush();
  const socket = ui.sockets[0];
  socket.onmessage({ data: JSON.stringify(events[0]) });
  assert.equal(ui.document.querySelectorAll('#eventList li').length, 55);
  const opened = ui.document.querySelector('#eventList details');
  const key = opened.dataset.eventKey;
  opened.open = true;
  socket.onmessage({ data: JSON.stringify({ ...events[0], event_id: 'latest-event', event_type: 'task.latest', created_at: '2026-08-31T16:00:00Z' }) });
  assert.equal([...ui.document.querySelectorAll('#eventList details')].find(n => n.dataset.eventKey === key).open, true);
  ui.click('#refreshRun'); await flush();
  assert.match(ui.document.querySelector('#eventList').textContent, /task.latest/);
  assert.equal(ui.sockets.length, 1);
});

test('a pending model configuration cannot restore model state after sign-out', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/config' ? pending : undefined });
  ui.click('#clearToken');
  release(new Response(JSON.stringify(fixture('/api/v1/models/config')))); await flush();
  assert.equal(ui.document.querySelector('#topbarModelName').textContent, 'Connect to configure');
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), null);
});

test('late approval completion cannot pull the user back from New run', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const approval = { approval_id: 'approval-1', status: 'PENDING', tool_name: 'git_push', call_hash: 'a'.repeat(64), expires_at: '2026-09-01T00:00:00Z' };
  const ui = await setup(t, { fetcher: path => path.endsWith('/decision') ? pending : path.endsWith('/approvals') ? new Response(JSON.stringify([approval])) : undefined });
  ui.click('[data-run-id]'); await flush();
  ui.click('#approvalList .button.primary');
  ui.document.querySelector('#approvalOperator').value = 'Test operator';
  ui.document.querySelector('#approvalForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.click('#closeApproval');
  ui.click('[data-nav="new"]');
  release(new Response('{}')); await flush();
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(ui.document.querySelector('#dashboard').classList.contains('hidden'), true);
});

test('a delayed folder upload cannot replace a newer connected repository', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const repo = name => ({ project_id: name, repository_id: name, name, source_path: name, default_branch: 'main', baseline_commit: 'a'.repeat(40) });
  const ui = await setup(t, { fetcher: (path, options) => {
    if (!path.endsWith('/onboard')) return;
    const payload = JSON.parse(options.body);
    return payload.files ? pending : new Response(JSON.stringify(repo('repo-b')));
  } });
  const picker = ui.document.querySelector('#dirPickerFallback');
  Object.defineProperty(picker, 'files', { value: [{ webkitRelativePath: 'upload-a/app.txt', size: 5, text: async () => 'hello' }] });
  picker.dispatchEvent(new ui.window.Event('change')); await flush();
  ui.document.querySelector('#projectName').value = 'repo-b';
  const source = ui.document.querySelector('#sourcePath'); source.value = 'repo-b'; source.dispatchEvent(new ui.window.Event('input'));
  ui.document.querySelector('#projectForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  release(new Response(JSON.stringify(repo('upload-a')))); await flush();
  assert.equal(JSON.parse(ui.window.sessionStorage.getItem('autoswe.repositorySelection')).repository_id, 'repo-b');
});

test('a previous file preview cannot overwrite the current file', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const files = ['file-a', 'file-b'].map(artifact_id => ({ artifact_id, sha256: 'a'.repeat(64), size_bytes: 10, media_type: 'text/plain' }));
  const ui = await setup(t, { fetcher: path => path.endsWith('/artifacts') ? new Response(JSON.stringify(files)) : path.endsWith('/artifacts/file-a') ? pending : path.endsWith('/artifacts/file-b') ? new Response('current file contents') : undefined });
  ui.click('[data-run-id]'); await flush();
  ui.click('.artifact-item');
  ui.click('#closeArtifactModal');
  ui.document.querySelectorAll('.artifact-item')[1].click(); await flush();
  release(new Response('old file contents')); await flush();
  assert.equal(ui.document.querySelector('#artifactPreviewCode').textContent, 'current file contents');
});

test('settings never silently reuse an autofilled token or save a launch dropdown change', async t => {
  const ui = await setup(t);
  ui.document.querySelector('#modelApiKey').value = 'autofilled-login-token';
  ui.click('#modelStudioBtn');
  assert.equal(ui.document.querySelector('#modelApiKey').value, '');
  ui.document.querySelector('#launchpadModelSelect').dispatchEvent(new ui.window.Event('change'));
  await flush();
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/models/config' && c.options.method === 'POST').length, 0);
});

test('Skip to content focuses the main area without changing the selected view', async t => {
  const ui = await setup(t);
  ui.click('[data-nav="new"]');
  ui.click('.skip-link');
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(ui.document.activeElement.id, 'mainContent');
});

test('Back and Forward restore routes without replacing an unfinished goal', { timeout: 3000 }, async t => {
  const ui = await setup(t);
  ui.click('[data-nav="new"]');
  ui.document.querySelector('#runGoal').value = 'Keep this unfinished goal';
  ui.click('[data-nav="runs"]'); await flush();
  const back = new Promise(resolve => ui.window.addEventListener('popstate', resolve, { once: true }));
  ui.window.history.back(); await back; await flush();
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(ui.document.querySelector('#onboardingSection').classList.contains('hidden'), false);
  assert.equal(ui.document.querySelector('#runGoal').value, 'Keep this unfinished goal');
  const forward = new Promise(resolve => ui.window.addEventListener('popstate', resolve, { once: true }));
  ui.window.history.forward(); await forward; await flush();
  assert.equal(ui.window.location.hash, '#runs');
  assert.equal(ui.document.querySelector('#onboardingSection').classList.contains('hidden'), true);
});

test('a late settings save cannot restore model state after sign-out', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: (path, options) => path === '/api/v1/models/config' && options.method === 'POST' ? pending : undefined });
  ui.click('#modelStudioBtn');
  ui.document.querySelector('#modelStudioForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.click('#closeModelStudio'); ui.click('#clearToken');
  release(new Response(JSON.stringify({ ...fixture('/api/v1/models/config'), primary_model: 'late-model' }))); await flush();
  assert.equal(ui.document.querySelector('#topbarModelName').textContent, 'Connect to configure');
});

test('opening settings discovers installed models and reopening refreshes the list', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? new Response(JSON.stringify({ reachable: true, models: ['installed-model'], latency_ms: 4 })) : undefined });
  ui.click('#modelStudioBtn'); await flush();
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/models/probe').length, 1);
  assert.match(ui.document.querySelector('#primaryModelSelect').textContent, /installed-model/);
  assert.equal(ui.document.querySelector('#discoveredModelsChips').textContent, 'installed-model');
  assert.match(ui.document.querySelector('#modelDiscoveryStatus').textContent, /1 model/);
  ui.click('#closeModelStudio'); ui.click('#modelStudioBtn'); await flush();
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/models/probe').length, 2);
  assert.match(ui.document.querySelector('#primaryModelSelect').textContent, /installed-model/);
});

test('choosing Ollama discovers real models without inserting a hardcoded model or fallback', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? new Response(JSON.stringify({ reachable: true, models: ['my-ollama-model'], latency_ms: 4 })) : undefined });
  ui.click('#modelStudioBtn'); await flush();
  ui.click('[data-provider="ollama"]'); await flush();
  assert.equal(ui.document.querySelector('#primaryModelInput').value, 'my-ollama-model');
  assert.equal(ui.document.querySelector('#fallbackModelsInput').value, '');
  assert.equal(ui.document.querySelector('#discoveredModelsChips').textContent, 'my-ollama-model');
});

test('endpoint edits clear discovered models and reject a delayed discovery response', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? pending : undefined });
  ui.click('#modelStudioBtn'); ui.click('#probeModelsBtn');
  const endpoint = ui.document.querySelector('#modelBaseUrl');
  endpoint.value = 'https://new-provider.example/v1';
  endpoint.dispatchEvent(new ui.window.Event('input'));
  release(new Response(JSON.stringify({ reachable: true, models: ['old-provider-model'], latency_ms: 4 }))); await flush();
  assert.doesNotMatch(ui.document.querySelector('#primaryModelSelect').textContent, /old-provider-model/);
  assert.equal(ui.document.querySelector('#discoveredModelsChips').textContent, '');
  assert.equal(ui.document.querySelector('#probeModelsBtn').disabled, false);
});

test('closing settings invalidates pending discovery, including after sign-out', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? pending : undefined });
  ui.click('#modelStudioBtn'); ui.click('#probeModelsBtn');
  ui.click('#closeModelStudio'); ui.click('#clearToken');
  release(new Response(JSON.stringify({ reachable: true, models: ['late-model'], latency_ms: 4 }))); await flush();
  assert.doesNotMatch(ui.document.querySelector('#primaryModelSelect').textContent, /late-model/);
  assert.equal(ui.document.querySelector('#modelStudioDialog').open, false);
});

test('empty discovery clears prior results and explains how to install Ollama models', async t => {
  let empty = false;
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? new Response(JSON.stringify({ reachable: true, models: empty ? [] : ['installed-model'], latency_ms: 4 })) : undefined });
  ui.click('#modelStudioBtn'); await flush();
  empty = true; ui.click('#probeModelsBtn'); await flush();
  assert.equal(ui.document.querySelector('#discoveredModelsChips').textContent, '');
  assert.match(ui.document.querySelector('#modelDiscoveryStatus').textContent, /No models.*ollama pull/i);
});

test('discovery failures remain visible in settings with a usable retry button', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? new Response(JSON.stringify({ reachable: false, models: [], latency_ms: 4, error: 'HTTP 401: authentication required' })) : undefined });
  ui.click('#modelStudioBtn'); await flush();
  assert.match(ui.document.querySelector('#modelDiscoveryStatus').textContent, /401/);
  assert.equal(ui.document.querySelector('#probeModelsBtn').disabled, false);
});

test('settings opened before configuration arrives gets the endpoint and starts discovery', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/config' ? pending : undefined });
  ui.click('#modelStudioBtn');
  release(new Response(JSON.stringify(fixture('/api/v1/models/config')))); await flush();
  assert.equal(ui.document.querySelector('#modelBaseUrl').value, 'http://localhost:11434/v1');
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/models/probe').length, 1);
});

test('editing a model cancels a pending connection test and removes its stale status', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/test' ? pending : undefined });
  ui.click('#modelStudioBtn'); await flush(); ui.click('#testModelBtn');
  const input = ui.document.querySelector('#primaryModelInput');
  input.value = 'different-model'; input.dispatchEvent(new ui.window.Event('input', { bubbles: true }));
  assert.equal(ui.document.querySelector('#modelTestCard').classList.contains('hidden'), true);
  assert.equal(ui.document.querySelector('#testModelBtn').disabled, false);
  release(new Response(JSON.stringify({ success: true, structured_output: true, latency_ms: 3 }))); await flush();
  assert.equal(ui.document.querySelector('#modelTestCard').classList.contains('hidden'), true);
});

test('selecting another discovered model clears the previous model verification', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/test' ? new Response(JSON.stringify({ success: true, structured_output: true, latency_ms: 3 })) : undefined });
  ui.click('#modelStudioBtn'); await flush(); ui.click('#testModelBtn'); await flush();
  ui.document.querySelectorAll('#discoveredModelsChips button')[1].click();
  assert.equal(ui.document.querySelector('#modelTestCard').classList.contains('hidden'), true);
});

test('a settings save cannot close or overwrite a newly opened settings draft', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: (path, options) => path === '/api/v1/models/config' && options.method === 'POST' ? pending : undefined });
  ui.click('#modelStudioBtn'); await flush();
  ui.document.querySelector('#modelStudioForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.click('#closeModelStudio'); ui.click('#modelStudioBtn'); await flush();
  const input = ui.document.querySelector('#primaryModelInput');
  input.value = 'unfinished-model'; input.dispatchEvent(new ui.window.Event('input', { bubbles: true }));
  release(new Response(JSON.stringify(fixture('/api/v1/models/config')))); await flush();
  assert.equal(ui.document.querySelector('#modelStudioDialog').open, true);
  assert.equal(input.value, 'unfinished-model');
});

test('a queued native close event cannot cancel discovery in a reopened dialog', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/probe' ? pending.then(response => response.clone()) : undefined });
  const dialog = ui.document.querySelector('#modelStudioDialog');
  dialog.close = function () { this.open = false; };
  ui.click('#modelStudioBtn'); ui.click('#closeModelStudio'); ui.click('#modelStudioBtn');
  dialog.dispatchEvent(new ui.window.Event('close'));
  release(new Response(JSON.stringify({ reachable: true, models: ['reopened-model'], latency_ms: 1 }))); await flush();
  assert.match(ui.document.querySelector('#discoveredModelsChips').textContent, /reopened-model/);
});


test('API-shaped task rows open details with the canonical ID, description and priority', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush();
  ui.click('.task-list-row'); await flush();
  assert.equal(ui.document.querySelector('#taskDrawer').open, true);
  assert.equal(ui.document.querySelector('#drawerTaskId').textContent, tasks[0].task_id);
  assert.equal(ui.document.querySelector('#drawerTaskGoal').textContent, tasks[0].description);
  assert.equal(ui.document.querySelector('#drawerTaskPriority').textContent, 'Priority 1');
  assert.ok(ui.calls.some(c => c.path === `/api/v1/projects/${runs[0].project_id}/tasks/${tasks[0].task_id}/messages?limit=30`));
});

test('task graphs use API dependencies to draw one task per stage and valid connectors', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush(); ui.click('#taskGraphView'); await flush();
  const columns = [...ui.document.querySelectorAll('.dag-column')];
  assert.equal(columns.length, 5);
  assert.deepEqual(columns.map(column => [...column.querySelectorAll('.task-node')].map(node => node.dataset.taskId)), tasks.map(task => [task.task_id]));
  assert.equal(ui.document.querySelectorAll('#dagSvgConnections path').length, 4);
});

test('live task sockets always use the canonical task UUID', async t => {
  const ui = await setup(t);
  ui.click('[data-run-id]'); await flush();
  assert.equal(ui.sockets[0].url, `ws://localhost/api/v1/projects/${runs[0].project_id}/tasks/${tasks[2].task_id}/events`);
});

test('task activity excludes run events and other tasks', async t => {
  const activity = [
    { ...events[0], event_id: 'run', event_type: 'run.requested', payload: {} },
    { ...events[0], event_id: 'this-task', event_type: 'task.selected', payload: { task_id: tasks[0].task_id } },
    { ...events[0], event_id: 'other-task', event_type: 'task.other', payload: { task_id: tasks[1].task_id } },
  ];
  const ui = await setup(t, { fetcher: path => path.includes('/events?') ? new Response(JSON.stringify(activity)) : undefined });
  ui.click('[data-run-id]'); await flush(); ui.click('#taskGraphView'); ui.click('.task-node'); await flush();
  assert.match(ui.document.querySelector('#drawerTaskEvents').textContent, /task.selected/);
  assert.doesNotMatch(ui.document.querySelector('#drawerTaskEvents').textContent, /run.requested|task.other/);
});

test('invalid run IDs show a concise lookup error without requests or navigation', async t => {
  const ui = await setup(t);
  ui.click('[data-nav="new"]');
  const input = ui.document.querySelector('#runLookup');
  input.value = 'not-a-run';
  ui.document.querySelector('#lookupForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.calls.filter(c => c.path.includes('/runs/not-a-run')).length, 0);
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  assert.match(ui.document.querySelector('#lookupFeedback').textContent, /valid run ID.*UUID/i);
  input.value = runs[0].run_id;
  ui.document.querySelector('#lookupForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.document.querySelector('#lookupFeedback').classList.contains('hidden'), true);
  assert.equal(input.hasAttribute('aria-invalid'), false);
  assert.equal(ui.document.querySelector('#runIdText').textContent, runs[0].run_id);
});

test('editing a connected repository clears the stale success feedback', async t => {
  const ui = await setup(t, { fetcher: path => path.endsWith('/onboard') ? new Response(JSON.stringify({ project_id: runs[0].project_id, repository_id: runs[0].repository_id, name: 'Checkout', source_path: 'checkout', default_branch: 'main', baseline_commit: 'a'.repeat(40) })) : undefined });
  ui.click('[data-nav="new"]');
  ui.document.querySelector('#projectName').value = 'Checkout';
  ui.document.querySelector('#sourcePath').value = 'checkout';
  ui.document.querySelector('#projectForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.match(ui.document.querySelector('#projectFeedback').textContent, /Repository connected/);
  const input = ui.document.querySelector('#sourcePath'); input.value = 'changed'; input.dispatchEvent(new ui.window.Event('input'));
  assert.equal(ui.document.querySelector('#projectFeedback').classList.contains('hidden'), true);
  assert.equal(ui.document.querySelector('#projectFeedback').classList.contains('success'), false);
});

for (const terminalState of ['FAILED', 'CANCELLED', 'COMPLETED']) {
  test(`${terminalState} runs with no tasks never show planning as current progress`, async t => {
    const ui = await setup(t, { fetcher: path => path === `/api/v1/runs/${runs[0].run_id}` ? new Response(JSON.stringify({ ...runs[0], state: terminalState, active_plan_revision: null, task_counts: {} })) : path.endsWith('/tasks') ? new Response('[]') : undefined });
    ui.click('[data-run-id]'); await flush();
    assert.doesNotMatch(ui.document.querySelector('#planRevision').textContent, /Planning/i);
    assert.match(ui.document.querySelector('#taskSummary').textContent, /No tasks/i);
    assert.match(ui.document.querySelector('#taskList').textContent, /No tasks/);
  });
}

test('model connection tests use the draft inference timeout', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/test' ? new Response(JSON.stringify({ success: false, latency_ms: 120000, error: 'Model test timed out after 120 seconds.' })) : undefined });
  ui.click('#modelStudioBtn'); await flush();
  ui.document.querySelector('#modelTimeoutInput').value = '120';
  ui.click('#testModelBtn'); await flush();
  assert.equal(JSON.parse(ui.calls.find(c => c.path === '/api/v1/models/test').options.body).timeout_seconds, 120);
  assert.match(ui.document.querySelector('#testOutputSnippet').textContent, /timed out after 120 seconds/);
});

test('changing the timeout aborts a pending model test and removes stale status', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path === '/api/v1/models/test' ? pending : undefined });
  ui.click('#modelStudioBtn'); await flush(); ui.click('#testModelBtn');
  const input = ui.document.querySelector('#modelTimeoutInput'); input.value = '120'; input.dispatchEvent(new ui.window.Event('input', { bubbles: true }));
  assert.equal(ui.calls.find(c => c.path === '/api/v1/models/test').options.signal.aborted, true);
  assert.equal(ui.document.querySelector('#modelTestCard').classList.contains('hidden'), true);
  release(new Response(JSON.stringify({ success: true, latency_ms: 2 }))); await flush();
  assert.equal(ui.document.querySelector('#modelTestCard').classList.contains('hidden'), true);
});

test('invalid timeout input blocks model tests with visible validation', async t => {
  const ui = await setup(t);
  ui.click('#modelStudioBtn'); await flush(); ui.document.querySelector('#modelTimeoutInput').value = '0';
  ui.click('#testModelBtn'); await flush();
  assert.equal(ui.calls.filter(c => c.path === '/api/v1/models/test').length, 0);
  assert.match(ui.document.querySelector('.toast-stack').textContent, /timeout.*10.*3600/i);
});

test('workspace connection stays pending and does not store a token before authentication succeeds', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const candidate = 'candidate-token-for-authentication-test';
  const ui = await setup(t, { token: false, fetcher: path => path === '/api/v1/runs?limit=1' ? pending : undefined });
  ui.click('#openAuth'); ui.document.querySelector('#adminToken').value = candidate;
  ui.document.querySelector('#authForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.document.querySelector('#authDialog').open, true);
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), null);
  assert.doesNotMatch(ui.document.querySelector('.toast-stack')?.textContent || '', /saved|connected/i);
  const request = ui.calls.find(c => c.path === '/api/v1/runs?limit=1');
  assert.equal(request.options.headers.get('Authorization'), `Bearer ${candidate}`);
  release(new Response('[]')); await flush();
  assert.equal(ui.document.querySelector('#authDialog').open, false);
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), candidate);
  assert.match(ui.document.querySelector('.toast-stack').textContent, /Workspace connected/);
});

test('rejected admin tokens keep the dialog open with no success message or stored credential', async t => {
  const ui = await setup(t, { token: false, fetcher: path => path.startsWith('/api/') ? new Response('{"detail":"Unauthorized"}', { status: 401 }) : undefined });
  ui.click('#openAuth'); ui.document.querySelector('#adminToken').value = 'invalid-token-for-authentication-test';
  ui.document.querySelector('#authForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.document.querySelector('#authDialog').open, true);
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), null);
  assert.match(ui.document.querySelector('#authFeedback').textContent, /rejected/i);
  assert.doesNotMatch(ui.document.querySelector('.toast-stack')?.textContent || '', /saved|connected/i);
});

test('closing workspace access prevents delayed authentication from signing in', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { token: false, fetcher: path => path === '/api/v1/runs?limit=1' ? pending : undefined });
  ui.click('#openAuth'); ui.document.querySelector('#adminToken').value = 'candidate-token-for-authentication-test';
  ui.document.querySelector('#authForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  ui.click('#closeAuth'); release(new Response('[]')); await flush();
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), null);
  assert.equal(ui.document.querySelector('#authDialog').open, false);
});

test('active run toolbar opens a named cancel dialog and submits only after confirmation', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  let cancelled = false;
  const ui = await setup(t, { fetcher: path => path.endsWith('/cancel') ? pending : cancelled && path === `/api/v1/runs/${runs[0].run_id}` ? new Response(JSON.stringify({ ...runs[0], state: 'CANCELLED' })) : undefined });
  ui.click('[data-run-id]'); await flush();
  const trigger = ui.document.querySelector('#cancelRun');
  assert.ok(trigger, 'Active runs expose a cancel button');
  assert.equal(trigger.classList.contains('hidden'), false);
  trigger.focus(); trigger.click();
  const dialog = ui.document.querySelector('#cancelRunDialog');
  assert.equal(dialog.open, true);
  assert.ok(dialog.getAttribute('aria-labelledby'));
  assert.match(ui.document.querySelector('#cancelRunDescription').textContent, new RegExp(runs[0].run_id));
  assert.equal(ui.document.activeElement.id, 'keepRun');
  assert.equal(ui.calls.filter(c => c.path.endsWith('/cancel')).length, 0);
  ui.document.querySelector('#cancelRunForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.document.querySelector('#cancelRunForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.calls.filter(c => c.path.endsWith('/cancel')).length, 1);
  assert.equal(ui.document.querySelector('#confirmCancelRun').disabled, true);
  cancelled = true; release(new Response('{}')); await flush();
  assert.equal(dialog.open, false);
  assert.equal(trigger.classList.contains('hidden'), true);
  assert.equal(ui.document.querySelector('#runState').textContent, 'Cancelled');
});

for (const dismissal of ['keepRun', 'closeCancelRun', 'Escape']) {
  test(`cancel dialog ${dismissal} dismisses without a request and restores toolbar focus`, async t => {
    const ui = await setup(t);
    ui.click('[data-run-id]'); await flush();
    const trigger = ui.document.querySelector('#cancelRun');
    assert.ok(trigger); trigger.focus(); trigger.click();
    if (dismissal === 'Escape') ui.document.activeElement.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    else ui.click(`#${dismissal}`);
    assert.equal(ui.document.querySelector('#cancelRunDialog').open, false);
    assert.equal(ui.document.activeElement, trigger);
    assert.equal(ui.calls.filter(c => c.path.endsWith('/cancel')).length, 0);
  });
}

test('a delayed cancellation cannot return the user to a run after navigation', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path.endsWith('/cancel') ? pending : undefined });
  ui.click('[data-run-id]'); await flush();
  assert.ok(ui.document.querySelector('#cancelRun')); ui.click('#cancelRun');
  ui.document.querySelector('#cancelRunForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.click('#closeCancelRun'); ui.click('[data-nav="new"]'); release(new Response('{}')); await flush();
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(ui.document.querySelector('#cancelRunDialog').open, false);
  assert.equal(ui.calls.filter(c => c.path === `/api/v1/runs/${runs[0].run_id}`).length, 1);
});

test('terminal runs do not offer cancellation in the toolbar or palette', async t => {
  const ui = await setup(t);
  ui.click(`[data-run-id="${runs[1].run_id}"]`); await flush();
  assert.ok(ui.document.querySelector('#cancelRun'));
  assert.equal(ui.document.querySelector('#cancelRun').classList.contains('hidden'), true);
  ui.click('#openCommandPalette');
  assert.doesNotMatch(ui.document.querySelector('#paletteList').textContent, /Cancel Current Run/);
});

test('the palette cancellation command opens the application dialog without window.confirm', async t => {
  const ui = await setup(t);
  let nativeConfirmCalls = 0;
  ui.window.confirm = () => { nativeConfirmCalls += 1; return false; };
  ui.click('[data-run-id]'); await flush(); ui.click('#openCommandPalette');
  [...ui.document.querySelectorAll('.palette-item')].find(item => /Cancel Current Run/.test(item.textContent)).click();
  assert.equal(nativeConfirmCalls, 0);
  assert.equal(ui.document.querySelector('#paletteDialog').open, false);
  assert.equal(ui.document.querySelector('#cancelRunDialog').open, true);
});

for (const [triggerId, dialogId, focusId] of [['tourHelpBtn', 'helpDialog', 'closeHelp'], ['modelStudioBtn', 'modelStudioDialog', 'closeModelStudio'], ['openCommandPalette', 'paletteDialog', 'paletteInput']]) {
  test(`${dialogId} Escape and native cancel restore focus and allow reopening`, async t => {
    const ui = await setup(t);
    const trigger = ui.document.querySelector(`#${triggerId}`);
    const dialog = ui.document.querySelector(`#${dialogId}`);
    trigger.focus(); trigger.click(); await flush();
    assert.equal(ui.document.activeElement.id, focusId);
    ui.document.activeElement.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    assert.equal(dialog.open, false);
    assert.equal(ui.document.activeElement, trigger);
    trigger.click(); await flush();
    dialog.dispatchEvent(new ui.window.Event('cancel', { cancelable: true }));
    assert.equal(dialog.open, false);
    assert.equal(ui.document.activeElement, trigger);
  });
}

test('closing and reopening authentication cannot accept an earlier pending attempt', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const candidate = 'candidate-token-for-authentication-test';
  const ui = await setup(t, { token: false, fetcher: path => path === '/api/v1/runs?limit=1' ? pending : undefined });
  const dialog = ui.document.querySelector('#authDialog');
  dialog.close = function () { this.open = false; }; // Native close events are queued.
  ui.click('#openAuth'); ui.document.querySelector('#adminToken').value = candidate;
  ui.document.querySelector('#authForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  dialog.dispatchEvent(new ui.window.Event('cancel', { cancelable: true }));
  ui.click('#openAuth'); ui.document.querySelector('#adminToken').value = candidate;
  dialog.dispatchEvent(new ui.window.Event('close'));
  release(new Response('[]')); await flush();
  assert.equal(ui.window.sessionStorage.getItem('autoswe.adminToken'), null);
  assert.equal(dialog.open, true);
});

test('queued task drawer close events cannot discard a newly requested task feed', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path.includes('/messages?') ? pending.then(response => response.clone()) : undefined });
  ui.click('[data-run-id]'); await flush();
  const dialog = ui.document.querySelector('#taskDrawer');
  dialog.close = function () { this.open = false; };
  ui.click('.task-list-row'); ui.click('#closeDrawer'); ui.click('.task-list-row');
  dialog.dispatchEvent(new ui.window.Event('close'));
  release(new Response(JSON.stringify([{ kind: 'context_handoff', sender: 'research', recipient: 'implementation', summary: 'Current task feed', created_at: events[0].created_at }]))); await flush();
  assert.match(ui.document.querySelector('#drawerTaskIntel').textContent, /Current task feed/);
});

test('pending cancellations remain deduplicated while moving between active runs', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const ui = await setup(t, { fetcher: path => path.endsWith('/cancel') ? pending.then(response => response.clone()) : path === `/api/v1/runs/${runs[1].run_id}` ? new Response(JSON.stringify({ ...runs[1], state: 'EXECUTING' })) : undefined });
  ui.click('[data-run-id]'); await flush(); ui.click('#cancelRun');
  ui.document.querySelector('#cancelRunForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true }));
  ui.click('#closeCancelRun');
  for (const run of [runs[1], runs[0]]) {
    ui.document.querySelector('#runLookup').value = run.run_id;
    ui.document.querySelector('#lookupForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
    ui.click('#cancelRun');
    if (ui.document.querySelector('#cancelRunDialog').open) {
      ui.document.querySelector('#cancelRunForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); ui.click('#closeCancelRun');
    }
  }
  assert.equal(ui.calls.filter(c => c.path === `/api/v1/runs/${runs[0].run_id}/cancel`).length, 1);
  release(new Response('{}')); await flush();
});

test('a queued artifact close does not abort a reopened preview', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const artifact = { artifact_id: 'file-a', sha256: 'a'.repeat(64), size_bytes: 10, media_type: 'text/plain' };
  const ui = await setup(t, { fetcher: path => path.endsWith('/artifacts') ? new Response(JSON.stringify([artifact])) : path.endsWith('/artifacts/file-a') ? pending.then(response => response.clone()) : undefined });
  ui.click('[data-run-id]'); await flush();
  const dialog = ui.document.querySelector('#artifactDialog'); dialog.close = function () { this.open = false; };
  ui.click('.artifact-item'); ui.click('#closeArtifactModal'); ui.click('.artifact-item');
  dialog.dispatchEvent(new ui.window.Event('close'));
  release(new Response('Reopened artifact content')); await flush();
  assert.equal(ui.document.querySelector('#artifactPreviewCode').textContent, 'Reopened artifact content');
});

test('a queued approval close cannot discard the currently open decision', async t => {
  const approval = { approval_id: 'approval-1', status: 'PENDING', tool_name: 'git_push', call_hash: 'a'.repeat(64), expires_at: '2026-09-01T00:00:00Z' };
  const ui = await setup(t, { fetcher: path => path.endsWith('/approvals') ? new Response(JSON.stringify([approval])) : path.endsWith('/decision') ? new Response('{}') : undefined });
  ui.click('[data-run-id]'); await flush();
  const dialog = ui.document.querySelector('#approvalDialog'); dialog.close = function () { this.open = false; };
  ui.click('#approvalList .button.primary'); ui.click('#closeApproval'); ui.click('#approvalList .button.primary');
  dialog.dispatchEvent(new ui.window.Event('close'));
  ui.document.querySelector('#approvalOperator').value = 'Test operator';
  ui.document.querySelector('#approvalForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.calls.filter(c => c.path.endsWith('/decision')).length, 1);
});

test('navigation clears transient lookup errors without clearing authentication or service feedback', async t => {
  const ui = await setup(t, { fetcher: path => path === '/api/v1/runs?limit=100'
    ? new Response('{"detail":"Service temporarily unavailable"}', { status: 503 })
    : path === '/api/v1/runs?limit=1' ? new Response('{"detail":"Unauthorized"}', { status: 401 }) : undefined });
  ui.click('#openAuth');
  ui.document.querySelector('#adminToken').value = 'invalid-token-for-authentication-test';
  ui.document.querySelector('#authForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  ui.click('#closeAuth');
  const authFeedback = ui.document.querySelector('#authFeedback').textContent;
  const serviceFeedback = ui.document.querySelector('#runsFeedback').textContent;
  assert.match(authFeedback, /rejected/i);
  assert.match(serviceFeedback, /Service temporarily unavailable/);
  const input = ui.document.querySelector('#runLookup');
  input.value = 'not-a-run';
  ui.document.querySelector('#lookupForm').dispatchEvent(new ui.window.Event('submit', { cancelable: true })); await flush();
  assert.equal(ui.document.querySelector('#lookupFeedback').classList.contains('hidden'), false);
  ui.click('[data-nav="new"]');
  assert.equal(ui.window.location.hash, '#new');
  assert.equal(ui.document.querySelector('#lookupFeedback').classList.contains('hidden'), true);
  assert.equal(ui.document.querySelector('#lookupFeedback').textContent, '');
  assert.equal(input.hasAttribute('aria-invalid'), false);
  assert.equal(ui.document.querySelector('#authFeedback').textContent, authFeedback);
  assert.equal(ui.document.querySelector('#runsFeedback').textContent, serviceFeedback);
});
