import { api, getToken } from './api.js?v=20260831-clean-ui';
import { el, escapeHtml, summarizeCounts, stateTone, timeAgo, formatCost, humanState } from './util.js?v=20260831-clean-ui';

const FILTERS = [['ALL', 'All'], ['ACTIVE', 'Active'], ['COMPLETED', 'Completed'], ['FAILED', 'Failed'], ['CANCELLED', 'Cancelled']];
const ACTIVE = new Set(['PENDING', 'PLANNING', 'EXECUTING', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_MEMORY']);
let filter = 'ALL';
let query = '';
let rows = [];
let loaded = false;
let visible = false;
let timer;
let request;
let hooks = {};
const cards = new Map();

function matches(run) {
  if (filter === 'ACTIVE' && !ACTIVE.has(run.state)) return false;
  if (filter !== 'ALL' && filter !== 'ACTIVE' && run.state !== filter) return false;
  return `${run.goal} ${run.project_name || ''} ${run.run_id}`.toLowerCase().includes(query.toLowerCase());
}
function rowContents(run) {
  const counts = summarizeCounts(run.task_counts);
  const pct = counts.total ? Math.round(counts.done / counts.total * 100) : 0;
  const project = run.project_name || `Project ${run.project_id?.slice(0, 8) || ''}`;
  const progress = counts.total ? `${counts.done} / ${counts.total}` : run.state === 'PLANNING' ? 'Planning' : 'No tasks';
  return `<div class="run-description"><span class="run-name">${escapeHtml(run.goal)}</span><span class="run-project"><span class="project-mark" aria-hidden="true">${escapeHtml(project[0])}</span>${escapeHtml(project)}</span></div>
    <span class="state-pill tone-${stateTone(run.state)}"><span class="pill-dot" aria-hidden="true"></span>${escapeHtml(humanState(run.state))}</span>
    <div class="run-task-progress" aria-label="${counts.done} of ${counts.total} tasks complete">${progress}<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div></div>
    <span class="run-cost">${formatCost(run.model_cost_usd)}</span><time class="run-time" datetime="${escapeHtml(run.created_at)}" title="${escapeHtml(new Date(run.created_at).toLocaleString())}">${timeAgo(run.created_at)}</time>`;
}
function summary() {
  const stats = [
    ['Total runs', rows.length, 'Across your projects'],
    ['In progress', rows.filter(r => ACTIVE.has(r.state)).length, 'Planning or executing'],
    ['Completed', rows.filter(r => r.state === 'COMPLETED').length, 'Work finished'],
    ['Needs attention', rows.filter(r => ['FAILED', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_MEMORY'].includes(r.state)).length, 'Failures or waiting for action'],
  ];
  const html = stats.map(([label, value, detail]) => `<div class="summary-stat"><span class="summary-stat-label">${label}</span><strong class="summary-stat-value">${value}</strong><p class="summary-stat-detail">${detail}</p></div>`).join('');
  if (el('runsSummary').innerHTML !== html) el('runsSummary').innerHTML = html;
}
function empty(title, message, action, label) {
  const grid = el('runsGrid');
  grid.innerHTML = `<div class="runs-empty"><div class="empty-glyph" aria-hidden="true">▤</div><h2>${title}</h2><p>${message}</p><button type="button" class="button primary" data-action="${action}">${label}</button></div>`;
}
function render() {
  const grid = el('runsGrid');
  summary();
  if (!getToken()) {
    el('runsCountLabel').textContent = 'Connect to see your runs';
    empty('Your workspace, in one place', 'Connect with your admin token to see existing work and start a new run.', 'connect', 'Connect workspace');
    return;
  }
  if (!loaded) return;
  const filtered = rows.filter(matches);
  el('runsCountLabel').textContent = `${filtered.length} of ${rows.length} loaded runs${rows.length === 100 ? ' · showing the latest 100' : ''}`;
  if (!rows.length) {
    empty('Ready for your first run?', 'Connect a repository and describe a change. You can follow its progress here.', 'new', 'Create a run');
    return;
  }
  if (!filtered.length) {
    empty('No matching runs', 'Try another search or clear the filters to see all your runs.', 'clear', 'Clear filters');
    return;
  }
  grid.querySelector('.runs-empty, .empty-state')?.remove();
  const keep = new Set(filtered.map(r => r.run_id));
  for (const child of [...grid.children]) if (!keep.has(child.dataset.rowId)) child.remove();
  filtered.forEach((run, index) => {
    let item = cards.get(run.run_id);
    if (!item) {
      item = document.createElement('div');
      item.setAttribute('role', 'listitem');
      item.dataset.rowId = run.run_id;
      const link = document.createElement('a');
      link.className = 'run-row';
      link.href = `#run/${encodeURIComponent(run.run_id)}`;
      link.dataset.runId = run.run_id;
      item.append(link);
      cards.set(run.run_id, item);
    }
    const link = item.firstElementChild;
    const html = rowContents(run);
    if (link.innerHTML !== html) link.innerHTML = html;
    link.setAttribute('aria-label', `Open run: ${run.goal}`);
    if (grid.children[index] !== item) grid.insertBefore(item, grid.children[index] || null);
  });
  for (const id of cards.keys()) if (!rows.some(r => r.run_id === id)) cards.delete(id);
}
function selectFilter(value) {
  filter = value;
  el('runsFilters').querySelectorAll('button').forEach(button => {
    const selected = button.dataset.filter === filter;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}
async function refresh() {
  if (!visible) return;
  if (!getToken()) { render(); return; }
  request?.abort();
  const current = new AbortController();
  request = current;
  el('runsRefresh').disabled = true;
  el('runsGrid').setAttribute('aria-busy', 'true');
  try {
    const data = await api('/api/v1/runs?limit=100', { signal: current.signal });
    if (request !== current || !visible || current.signal.aborted) return;
    rows = data || [];
    loaded = true;
    el('runsFeedback').classList.add('hidden');
    render();
  } catch (error) {
    if (current.signal.aborted || request !== current || !visible) return;
    el('runsFeedback').textContent = `Could not refresh runs. ${error.message} Use Refresh to try again.`;
    el('runsFeedback').classList.remove('hidden');
    if (!loaded) empty('Runs couldn’t be loaded', 'Check your connection and workspace access, then try again.', 'retry', 'Try again');
  } finally {
    if (request === current) {
      request = null;
      el('runsRefresh').disabled = false;
      el('runsGrid').setAttribute('aria-busy', 'false');
      schedule();
    }
  }
}
function schedule() {
  window.clearTimeout(timer);
  if (visible && getToken()) timer = window.setTimeout(() => {
    if (document.hidden) schedule();
    else void refresh();
  }, 5000);
}
export function initRunsBrowser(options) {
  hooks = options;
  el('runsFilters').innerHTML = FILTERS.map(([id, label]) => `<button class="filter-chip${id === filter ? ' active' : ''}" type="button" data-filter="${id}" aria-pressed="${id === filter}">${label}</button>`).join('');
  el('runsFilters').addEventListener('click', event => {
    const button = event.target.closest('[data-filter]');
    if (button) { selectFilter(button.dataset.filter); render(); }
  });
  el('runsSearch').addEventListener('input', event => { query = event.target.value.trim(); render(); });
  el('runsRefresh').addEventListener('click', () => void refresh());
  el('runsGrid').addEventListener('click', event => {
    const link = event.target.closest('[data-run-id]');
    if (link) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      hooks.onOpenRun(link.dataset.runId);
      return;
    }
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (action === 'connect') hooks.onConnect();
    if (action === 'new') hooks.onNewRun();
    if (action === 'retry') void refresh();
    if (action === 'clear') { selectFilter('ALL'); query = ''; el('runsSearch').value = ''; render(); }
  });
}
export function showRunsBrowser() {
  visible = true;
  render();
  void refresh();
}
export function hideRunsBrowser() {
  visible = false;
  window.clearTimeout(timer);
  request?.abort();
  request = null;
  el('runsRefresh').disabled = false;
  el('runsGrid').setAttribute('aria-busy', 'false');
}
export function resetRunsBrowser() {
  hideRunsBrowser();
  rows = []; loaded = false; cards.clear();
  el('runsGrid').replaceChildren();
  el('runsFeedback').classList.add('hidden');
}
