// tour.js — guided onboarding tour with spotlight and persistence.
// No external deps. Persists "seen" in localStorage.

const STORAGE_KEY = 'autoswe.tourSeen.v2';
const STEPS = [
  {
    target: '[data-nav="runs"]',
    title: 'Runs Browser',
    body: 'Every engineering run across projects. Filter by status, search by goal, and jump into any mission. This is your new home.',
    placement: 'bottom',
  },
  {
    target: '#runsGrid, #taskDag',
    title: 'Live System',
    body: 'Cards pulse when active. The DAG animates along running edges. The HUD sparkline shows cumulative cost building in real time.',
    placement: 'bottom',
  },
  {
    target: '#onboardingSection, #projectForm',
    title: 'Launch a Mission',
    body: 'Pick a repository folder, set a baseline commit, describe your goal. The planner builds a typed task DAG — independent work runs in parallel.',
    placement: 'top',
  },
  {
    target: '.task-dag-container, #dagViewport',
    title: 'Task DAG',
    body: 'Each node is a task. Click to inspect agent reasoning, approvals, and artifacts. Stale results are auto-replayed when the worktree changed.',
    placement: 'top',
  },
  {
    target: '#eventList, .terminal-container',
    title: 'Audit Timeline',
    body: 'Immutable audit trail. Filter by task / tool / approval, search payloads, and expand JSON. Virtualized for large runs — hover to pause auto-scroll.',
    placement: 'top',
  },
  {
    target: '#modelStudioBtn, #openAuth',
    title: 'Model & Auth',
    body: 'Configure providers and rotate your admin token. Press ⌘K to open the command palette from anywhere.',
    placement: 'bottom',
  },
];

let current = 0;
let overlay = null;
let card = null;

function hasSeen() {
  try { return localStorage.getItem(STORAGE_KEY) === '1'; } catch (_) { return true; }
}
function markSeen() {
  try { localStorage.setItem(STORAGE_KEY, '1'); } catch (_) {}
}

function ensureOverlay() {
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  overlay.innerHTML = `
    <div class="tour-spotlight" id="tourSpotlight"></div>
    <div class="tour-card" id="tourCard" role="dialog" aria-modal="true" aria-labelledby="tourTitle">
      <div class="tour-progress" id="tourProgress"></div>
      <h3 id="tourTitle" class="tour-title"></h3>
      <p id="tourBody" class="tour-body"></p>
      <div class="tour-actions">
        <button id="tourSkip" class="button ghost" type="button">Skip</button>
        <div class="tour-step-actions">
          <button id="tourPrev" class="button ghost" type="button">Back</button>
          <button id="tourNext" class="button primary" type="button">Next →</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  card = overlay.querySelector('#tourCard');
  overlay.querySelector('#tourSkip').addEventListener('click', endTour);
  overlay.querySelector('#tourPrev').addEventListener('click', () => goTo(current - 1));
  overlay.querySelector('#tourNext').addEventListener('click', () => goTo(current + 1));
  overlay.addEventListener('click', (e) => { if (e.target === overlay) endTour(); });
  document.addEventListener('keydown', onKey);
  return overlay;
}

function onKey(e) {
  if (!overlay || overlay.style.display === 'none') return;
  if (e.key === 'Escape') endTour();
  if (e.key === 'ArrowRight') goTo(current + 1);
  if (e.key === 'ArrowLeft') goTo(current - 1);
}

function positionSpotlight(targetEl) {
  const spot = document.getElementById('tourSpotlight');
  const cardEl = document.getElementById('tourCard');
  if (!targetEl || !spot || !cardEl) return;

  const rect = targetEl.getBoundingClientRect();
  const pad = 8;
  spot.style.left = `${Math.max(8, rect.left - pad)}px`;
  spot.style.top = `${Math.max(8, rect.top - pad)}px`;
  spot.style.width = `${rect.width + pad * 2}px`;
  spot.style.height = `${rect.height + pad * 2}px`;

  // Position card near target, flip if near viewport edge
  const cardRect = cardEl.getBoundingClientRect();
  let top = rect.bottom + 16;
  let left = rect.left;
  if (top + cardRect.height > window.innerHeight - 16) {
    top = rect.top - cardRect.height - 16;
  }
  if (left + cardRect.width > window.innerWidth - 16) {
    left = window.innerWidth - cardRect.width - 16;
  }
  left = Math.max(16, left);
  top = Math.max(16, top);
  cardEl.style.left = `${left}px`;
  cardEl.style.top = `${top}px`;
  cardEl.style.right = 'auto';
  cardEl.style.bottom = 'auto';
}

function goTo(index) {
  if (index >= STEPS.length) {
    endTour();
    return;
  }
  if (index < 0) return;
  current = index;
  const step = STEPS[current];
  const targetEl = document.querySelector(step.target);
  const titleEl = document.getElementById('tourTitle');
  const bodyEl = document.getElementById('tourBody');
  const progressEl = document.getElementById('tourProgress');
  const prevBtn = document.getElementById('tourPrev');
  const nextBtn = document.getElementById('tourNext');

  // Fallback: skip step if target not in DOM (e.g., hidden section)
  if (!targetEl) {
    if (current < STEPS.length - 1) { current += 1; goTo(current); return; }
    endTour(); return;
  }

  ensureOverlay();
  overlay.style.display = 'block';
  titleEl.textContent = step.title;
  bodyEl.textContent = step.body;
  progressEl.textContent = `${current + 1} / ${STEPS.length}`;
  prevBtn.style.visibility = current === 0 ? 'hidden' : 'visible';
  nextBtn.textContent = current === STEPS.length - 1 ? 'Done ✓' : 'Next →';
  requestAnimationFrame(() => positionSpotlight(targetEl));
}

function startTour() {
  current = 0;
  goTo(0);
}

function endTour() {
  if (overlay) overlay.style.display = 'none';
  document.removeEventListener('keydown', onKey);
  markSeen();
}

export function initTour() {
  // Expose manual trigger
  const btn = document.getElementById('tourHelpBtn');
  if (btn) btn.addEventListener('click', startTour);

  // Auto-start once for first-time visitors, after initial paint
  if (!hasSeen()) {
    window.setTimeout(startTour, 1200);
  }
}

export function resetTour() {
  try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
}
