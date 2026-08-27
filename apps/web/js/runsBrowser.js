// Runs Browser: cross-project run overview with live status pills.

import { api } from './api.js';
import { el, escapeHtml, summarizeCounts, stateTone, timeAgo, formatCost, relativeTimeShort } from './util.js';
import { showToast } from './toast.js';
import { renderSparkline, fetchHistory } from './sparkline.js';

const FILTERS = [
  { id: 'ALL', label: 'All' },
  { id: 'ACTIVE', label: 'Active' },
  { id: 'COMPLETED', label: 'Completed' },
  { failed: true, id: 'FAILED', label: 'Failed' },
];

const ACTIVE_STATES = new Set(['PENDING', 'PLANNING', 'EXECUTING', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_MEMORY']);

let currentFilter = 'ALL';
let searchQuery = '';
let rows = [];
let pollTimer = null;
let visible = false;
let hooks = { onOpenRun: () => {}, onNewRun: () => {} };

function matches(run) {
  if (currentFilter === 'ACTIVE' && !ACTIVE_STATES.has(run.state)) return false;
  if (currentFilter === 'COMPLETED' && run.state !== 'COMPLETED') return false;
  if (currentFilter === 'FAILED' && run.state !== 'FAILED') return false;
  if (searchQuery) {
    const needle = searchQuery.toLowerCase();
    const haystack = `${run.goal} ${run.project_name || ''} ${run.run_id}`.toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

function progressFor(run) {
  const counts = summarizeCounts(run.task_counts);
  if (!counts.total) return { pct: 0, label: 'planning' };
  const pct = Math.round((counts.done / counts.total) * 100);
  return { pct, label: `${counts.done}/${counts.total} tasks` };
}

function cardHtml(run) {
  const tone = stateTone(run.state);
  const progress = progressFor(run);
  const active = ACTIVE_STATES.has(run.state);
  return `
    <article class="run-card" data-run-id="${escapeHtml(run.run_id)}" tabindex="0" role="button"
             aria-label="Open run ${escapeHtml(run.goal)}">
      <header class="run-card-head">
        <span class="state-pill tone-${tone}${active ? ' pulse' : ''}">
          <span class="pill-dot"></span>${escapeHtml(run.state)}
        </span>
        <span class="run-card-time" title="${escapeHtml(run.created_at)}">${timeAgo(run.created_at)}</span>
      </header>
      <h3 class="run-card-goal">${escapeHtml(run.goal.length > 140 ? `${run.goal.slice(0, 140)}…` : run.goal)}</h3>
      <div class="run-card-meta">
        <span class="meta-chip" title="Project">${escapeHtml(run.project_name || 'Project')}</span>
        <span class="meta-chip mono">rev ${escapeHtml(String(run.active_plan_revision ?? '—'))}</span>
        <span class="meta-chip mono">${escapeHtml(relativeTimeShort(run.created_at))} old</span>
      </div>
      <div class="run-card-progress">
        <div class="progress-track"><div class="progress-fill tone-${tone}" style="width:${progress.pct}%"></div></div>
        <span class="progress-label">${progress.label}</span>
      </div>
      <div class="run-sparkline" data-run-id="${escapeHtml(run.run_id)}" title="Cost history"></div>
      <footer class="run-card-foot">
        <span class="foot-stat" title="Model tokens">
          <span class="foot-icon">◈</span>${(run.model_input_tokens + run.model_output_tokens).toLocaleString()}
        </span>
        <span class="foot-stat" title="Cost">
          <span class="foot-icon">$</span>${formatCost(run.model_cost_usd)}
        </span>
        <span class="foot-stat fail-stat${run.task_counts.FAILED ? ' has-fails' : ''}" title="Failed tasks">
          <span class="foot-icon">⚑</span>${Number(run.task_counts.FAILED || 0)}
        </span>
        <button class="run-open-btn" type="button" data-run-id="${escapeHtml(run.run_id)}">Open →</button>
      </footer>
    </article>`;
}

function renderHero() {
  const hero = el('runsHero');
  if (!hero) return;
  const active = rows.find((r) => ACTIVE_STATES.has(r.state));
  if (!active) {
    hero.classList.add('hidden');
    hero.innerHTML = '';
    return;
  }
  hero.classList.remove('hidden');
  const prog = progressFor(active);
  hero.innerHTML = `
    <div class="runs-hero-card">
      <span class="runs-hero-label"><i></i> Active Mission</span>
      <h3 class="runs-hero-goal">${escapeHtml(active.goal.slice(0, 160))}</h3>
      <div class="runs-hero-meta">
        <span class="meta-chip">${escapeHtml(active.project_name || 'Project')}</span>
        <span class="meta-chip mono">${escapeHtml(active.state)}</span>
        <span class="meta-chip mono">${escapeHtml(timeAgo(active.created_at))}</span>
      </div>
      <div class="run-card-progress" style="margin-top:10px">
        <div class="progress-track"><div class="progress-fill tone-${stateTone(active.state)}" style="width:${prog.pct}%"></div></div>
        <span class="progress-label">${prog.label}</span>
      </div>
    </div>
    <div class="runs-hero-card" style="display:flex;flex-direction:column;justify-content:center;gap:10px">
      <div class="hud-header"><span class="hud-label">Live Fleet</span><span class="hud-icon">◈</span></div>
      <div style="display:flex;gap:18px">
        <div><div class="hud-main-val" style="font-size:1.4rem">${rows.filter((r) => ACTIVE_STATES.has(r.state)).length}</div><div class="hud-subtext">active</div></div>
        <div><div class="hud-main-val" style="font-size:1.4rem">${rows.length}</div><div class="hud-subtext">total runs</div></div>
        <div><div class="hud-main-val text-emerald" style="font-size:1.15rem">${formatCost(rows.reduce((s, r) => s + Number(r.model_cost_usd || 0), 0))}</div><div class="hud-subtext">fleet cost</div></div>
      </div>
    </div>`;
}

function renderTicker() {
  const ticker = el('liveTicker');
  const track = el('liveTickerTrack');
  if (!ticker || !track) return;
  if (!rows.length) { ticker.classList.add('hidden'); return; }
  ticker.classList.remove('hidden');
  const activeCount = rows.filter((r) => ACTIVE_STATES.has(r.state)).length;
  const totalCost = formatCost(rows.reduce((s, r) => s + Number(r.model_cost_usd || 0), 0));
  const segs = [
    `${rows.length} missions`,
    `${activeCount} active`,
    `fleet cost ${totalCost}`,
    `${rows.filter((r) => r.state === 'COMPLETED').length} shipped`,
    `${rows.filter((r) => r.state === 'FAILED').length} failed`,
  ];
  const html = segs.map((s) => `<span>● ${escapeHtml(s)}</span>`).join('');
  track.innerHTML = html + html; // doubled for seamless loop
}

function render() {
  const grid = el('runsGrid');
  if (!grid) return;
  const filtered = rows.filter(matches);
  el('runsCountLabel').textContent = `${filtered.length} run${filtered.length === 1 ? '' : 's'}`;
  renderHero();
  renderTicker();

  if (!rows.length) {
    grid.innerHTML = `
      <div class="runs-empty">
        <div class="empty-glyph">◇</div>
        <h3>No engineering runs yet</h3>
        <p>Launch your first mission from the console — AutoSWE plans a typed task DAG,
           executes it with governed agents, and ships only verified work.</p>
        <button class="button primary" id="runsEmptyCta" type="button">＋ New Mission</button>
      </div>`;
    el('runsEmptyCta')?.addEventListener('click', () => hooks.onNewRun());
    return;
  }

  if (!filtered.length) {
    grid.innerHTML = `<div class="runs-empty slim"><div class="empty-glyph">∅</div><p>No runs match this filter.</p></div>`;
    return;
  }

  grid.innerHTML = filtered.map(cardHtml).join('');
  // Lazy sparkline hydration — IntersectionObserver avoids thundering herd
  const sparklines = grid.querySelectorAll('.run-sparkline[data-run-id]');
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const el2 = entry.target;
        io.unobserve(el2);
        const rid = el2.getAttribute('data-run-id');
        fetchHistory(rid).then((samples) => renderSparkline(el2, samples, { width: 110, height: 26 }));
      }
    }, { rootMargin: '100px' });
    sparklines.forEach((el2) => io.observe(el2));
  } else {
    sparklines.forEach((el2) => {
      fetchHistory(el2.getAttribute('data-run-id')).then((samples) => renderSparkline(el2, samples, { width: 110, height: 26 }));
    });
  }
}

async function refresh({ silent = false } = {}) {
  try {
    rows = await api('/api/v1/runs?limit=100');
    render();
  } catch (error) {
    if (!silent) showToast(error.message, true);
  }
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (!visible) return;
  pollTimer = window.setTimeout(async () => {
    if (visible) await refresh({ silent: true });
    schedulePoll();
  }, 4000);
}

export function initRunsBrowser(options = {}) {
  hooks = { ...hooks, ...options };
  const bar = el('runsFilters');
  if (bar) {
    bar.innerHTML = FILTERS.map(
      (filter) =>
        `<button type="button" class="filter-chip${filter.id === currentFilter ? ' active' : ''}" data-filter="${filter.id}">${filter.label}</button>`
    ).join('');
    bar.querySelectorAll('.filter-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        currentFilter = chip.dataset.filter;
        bar.querySelectorAll('.filter-chip').forEach((other) => other.classList.toggle('active', other === chip));
        render();
      });
    });
  }
  el('runsSearch')?.addEventListener('input', (event) => {
    searchQuery = event.target.value.trim();
    render();
  });
  el('runsRefresh')?.addEventListener('click', () => void refresh());
  // Delegated open — works for cards added after render, and for the inner Open button
  const gridEl = el('runsGrid');
  if (gridEl && !gridEl._openBound) {
    gridEl._openBound = true;
    gridEl.addEventListener('click', (event) => {
      const btn = event.target.closest('.run-open-btn');
      if (btn && btn.dataset.runId) {
        event.preventDefault();
        hooks.onOpenRun(btn.dataset.runId);
        return;
      }
      const card = event.target.closest('.run-card');
      if (card && card.dataset.runId) {
        hooks.onOpenRun(card.dataset.runId);
      }
    });
    gridEl.addEventListener('keydown', (event) => {
      const card = event.target.closest('.run-card');
      if (!card || !card.dataset.runId) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        hooks.onOpenRun(card.dataset.runId);
      }
    });
  }
}

export function showRunsBrowser() {
  visible = true;
  el('runsBrowserSection')?.classList.remove('hidden');
  el('dashboard')?.classList.add('hidden');
  el('launchpadSection')?.classList.add('hidden');
  document.querySelectorAll('[data-nav]').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.nav === 'runs');
  });
  void refresh();
  schedulePoll();
}

export function hideRunsBrowser() {
  visible = false;
  window.clearTimeout(pollTimer);
  el('runsBrowserSection')?.classList.add('hidden');
}
