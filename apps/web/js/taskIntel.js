// Agent reasoning feed for the task drawer: newest handoff summaries.

import { api } from './api.js?v=20260831-clean-ui';
import { escapeHtml, timeAgo } from './util.js?v=20260831-clean-ui';

const KIND_META = {
  context_handoff: { label: 'Handoff', icon: '⤷', tone: 'cyan' },
  research_evidence: { label: 'Evidence', icon: '◈', tone: 'sky' },
  patch_proposal: { label: 'Patch', icon: '✎', tone: 'violet' },
  test_evidence: { label: 'Tests', icon: '✓', tone: 'emerald' },
  review_finding: { label: 'Review', icon: '⚑', tone: 'amber' },
  validation_result: { label: 'Validation', icon: '◎', tone: 'emerald' },
  blocker: { label: 'Blocker', icon: '✕', tone: 'rose' },
  task_completion: { label: 'Completion', icon: '◆', tone: 'emerald' },
};

let loadToken = 0;

export async function loadTaskIntel(projectId, taskId, container) {
  if (!container) return;
  const token = ++loadToken;
  container.classList.remove('hidden');
  container.innerHTML = `
    <div class="intel-skeleton">
      <div class="skeleton-line w60"></div>
      <div class="skeleton-line w90"></div>
      <div class="skeleton-line w80"></div>
    </div>`;

  let messages;
  try {
    messages = await api(
      `/api/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/messages?limit=30`
    );
  } catch (error) {
    if (token !== loadToken) return;
    container.innerHTML = `<p class="intel-error">Agent feed unavailable — ${escapeHtml(error.message)}</p>`;
    return;
  }
  if (token !== loadToken) return;

  if (!messages.length) {
    container.innerHTML = `
      <p class="intel-empty">No agent handoffs recorded for this task yet.</p>`;
    return;
  }

  const items = messages
    .map((message) => {
      const meta = KIND_META[message.kind] || { label: message.kind, icon: '›', tone: 'muted' };
      return `
        <li class="intel-item tone-${meta.tone}">
          <span class="intel-icon" aria-hidden="true">${meta.icon}</span>
          <div class="intel-body">
            <header class="intel-head">
              <span class="intel-kind">${escapeHtml(meta.label)}</span>
              <span class="intel-sender">${escapeHtml(message.sender)} → ${escapeHtml(message.recipient)}</span>
              <time class="intel-time" title="${escapeHtml(message.created_at)}">${timeAgo(message.created_at)}</time>
            </header>
            <p class="intel-summary">${escapeHtml(message.summary.length > 900 ? `${message.summary.slice(0, 900)}…` : message.summary)}</p>
          </div>
        </li>`;
    })
    .join('');

  container.innerHTML = `<ol class="intel-list">${items}</ol>`;
}

export function clearTaskIntel(container) {
  if (!container) return;
  loadToken += 1;
  container.innerHTML = '';
  container.classList.add('hidden');
}
