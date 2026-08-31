import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, cp, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';
import { fixture, runs, events } from './fixtures.mjs';

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
  Object.assign(globalThis, { window, document: window.document, sessionStorage: window.sessionStorage, localStorage: window.localStorage, fetch, ResizeObserver: window.ResizeObserver, requestAnimationFrame: cb => { cb(); return 0; }, cancelAnimationFrame() {}, WebSocket: class { constructor() { sockets.push(this); } close() { this.onclose?.(); } } });
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
