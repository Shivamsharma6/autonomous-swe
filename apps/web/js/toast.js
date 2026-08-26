// Stacked toast notifications with severity styling.

import { el } from './util.js';

let host = null;

function ensureHost() {
  if (host && document.body.contains(host)) return host;
  host = document.createElement('div');
  host.className = 'toast-stack';
  host.setAttribute('aria-live', 'polite');
  document.body.appendChild(host);
  return host;
}

export function showToast(message, isError = false, { timeout = 4200 } = {}) {
  const stack = ensureHost();
  const legacy = el('toast');
  if (legacy) legacy.classList.add('hidden');

  const item = document.createElement('div');
  item.className = `toast-item${isError ? ' error' : ''}`;
  item.setAttribute('role', isError ? 'alert' : 'status');
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.textContent = isError ? '✕' : '✓';
  const text = document.createElement('span');
  text.className = 'toast-text';
  text.textContent = String(message ?? '');
  item.append(icon, text);
  stack.appendChild(item);

  requestAnimationFrame(() => item.classList.add('visible'));
  window.setTimeout(() => {
    item.classList.remove('visible');
    window.setTimeout(() => item.remove(), 260);
  }, timeout);
  return item;
}
