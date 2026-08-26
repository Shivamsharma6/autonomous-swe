// Shared DOM + formatting utilities for the control plane.

export const el = (id) => document.getElementById(id);

export const terminalStates = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);

export function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatDuration(seconds) {
  const value = Number(seconds) || 0;
  if (value < 60) return `${value.toFixed(0)}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatCost(usd) {
  return `$${(Number(usd) || 0).toFixed(4)}`;
}

export function timeAgo(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return '—';
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return 'just now';
  if (seconds < 90) return '1 min ago';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function summarizeCounts(counts = {}) {
  const total = Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
  const done = Number(counts.COMPLETED || 0);
  const failed = Number(counts.FAILED || 0);
  const active = Number(counts.RUNNING || 0) + Number(counts.LEASED || 0);
  return { total, done, failed, active };
}

export function stateTone(state) {
  switch (state) {
    case 'COMPLETED': return 'completed';
    case 'FAILED': return 'failed';
    case 'RUNNING': return 'running';
    case 'LEASED': return 'running';
    case 'READY': return 'ready';
    case 'WAITING_FOR_APPROVAL': return 'warning';
    case 'WAITING_FOR_MEMORY': return 'warning';
    case 'CANCELLED': return 'cancelled';
    default: return 'pending';
  }
}

export function relativeTimeShort(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return '—';
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const area = document.createElement('textarea');
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand('copy');
  area.remove();
  return Promise.resolve();
}
