// Keyboard-first command palette (⌘K / /): fuzzy actions, runs, projects.

import { el, escapeHtml } from './util.js?v=20260831-clean-ui';

let actions = [];
let filtered = [];
let selectedIndex = 0;
let dialog = null;
let listEl = null;
let inputEl = null;
let getActions = () => [];

function fuzzyScore(query, text) {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 1;
  let score = 0;
  let qi = 0;
  let streak = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti += 1) {
    if (t[ti] === q[qi]) {
      streak += 1;
      score += 2 + streak + (ti === 0 || t[ti - 1] === ' ' ? 4 : 0);
      qi += 1;
    } else {
      streak = 0;
    }
  }
  if (qi < q.length) return 0;
  return score / t.length;
}

function highlight(title, query) {
  if (!query) return escapeHtml(title);
  const loweredTitle = title.toLowerCase();
  const loweredQuery = query.toLowerCase();
  let out = '';
  let qi = 0;
  for (let i = 0; i < title.length; i += 1) {
    if (qi < loweredQuery.length && loweredTitle[i] === loweredQuery[qi]) {
      out += `<mark>${escapeHtml(title[i])}</mark>`;
      qi += 1;
    } else {
      out += escapeHtml(title[i]);
    }
  }
  return out;
}

function renderList() {
  const query = inputEl.value.trim();
  filtered = actions
    .map((action) => ({ action, score: fuzzyScore(query, `${action.title} ${action.keywords || ''}`) }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 12);
  selectedIndex = Math.min(selectedIndex, Math.max(0, filtered.length - 1));

  if (!filtered.length) {
    listEl.innerHTML = `<div class="palette-empty">No matches for “${escapeHtml(query)}”</div>`;
    return;
  }
  listEl.innerHTML = filtered
    .map((entry, index) => {
      const action = entry.action;
      return `
        <button type="button" class="palette-item${index === selectedIndex ? ' selected' : ''}" data-index="${index}">
          <span class="palette-icon">${action.icon || '›'}</span>
          <span class="palette-title">${highlight(action.title, query)}</span>
          ${action.hint ? `<span class="palette-hint">${escapeHtml(action.hint)}</span>` : ''}
        </button>`;
    })
    .join('');
  listEl.querySelectorAll('.palette-item').forEach((item) => {
    item.addEventListener('click', () => execute(Number(item.dataset.index)));
    item.addEventListener('mousemove', () => {
      const index = Number(item.dataset.index);
      if (index !== selectedIndex) {
        selectedIndex = index;
        listEl.querySelectorAll('.palette-item').forEach((other, otherIndex) => {
          other.classList.toggle('selected', otherIndex === selectedIndex);
        });
      }
    });
  });
  listEl.querySelector('.palette-item.selected')?.scrollIntoView({ block: 'nearest' });
}

function execute(index) {
  const entry = filtered[index];
  if (!entry) return;
  dialog.close();
  try {
    entry.action.run();
  } catch (error) {
    // Action handlers own their error reporting; palette stays inert.
  }
}

export function initPalette(registerActions) {
  dialog = el('paletteDialog');
  inputEl = el('paletteInput');
  listEl = el('paletteList');
  if (!dialog || !inputEl || !listEl) return;

  getActions = registerActions;

  inputEl.addEventListener('input', () => {
    selectedIndex = 0;
    renderList();
  });
  inputEl.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      selectedIndex = Math.min(selectedIndex + 1, filtered.length - 1);
      renderList();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      selectedIndex = Math.max(selectedIndex - 1, 0);
      renderList();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      execute(selectedIndex);
    }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });

  window.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || document.querySelector('dialog[open]')) return;
    const isK = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k';
    const isSlash = event.key === '/' && !event.target.closest('input, textarea, select, [contenteditable]');
    if (isK || isSlash) {
      event.preventDefault();
      openPalette();
    }
  });
}

export function openPalette() {
  if (!dialog) return;
  actions = getActions();
  inputEl.value = '';
  selectedIndex = 0;
  renderList();
  if (!dialog.open) dialog.showModal();
}
