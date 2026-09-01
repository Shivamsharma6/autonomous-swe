import { api, setUnauthorizedHandler, getToken, setToken, clearToken, connectTaskSocket } from './js/api.js?v=20260831-clean-ui';
import {
  el,
  terminalStates,
  escapeHtml,
  formatDuration,
  formatBytes,
  summarizeCounts,
  stateTone,
  copyToClipboard,
  humanState,
} from './js/util.js?v=20260831-clean-ui';
import { showToast } from './js/toast.js?v=20260831-clean-ui';
import { initRunsBrowser, showRunsBrowser, hideRunsBrowser, resetRunsBrowser } from './js/runsBrowser.js?v=20260831-clean-ui';
import { initPalette, openPalette } from './js/palette.js?v=20260831-clean-ui';
import { loadTaskIntel, clearTaskIntel } from './js/taskIntel.js?v=20260831-clean-ui';
import { initTour } from './js/tour.js?v=20260831-clean-ui';
import { showDialog, closeDialog } from './js/dialogs.js?v=20260831-clean-ui';

(() => {
  'use strict';

  // Global State
  const state = {
    token: sessionStorage.getItem('autoswe.adminToken') || '',
    projectId: sessionStorage.getItem('autoswe.projectId') || '',
    repositoryId: sessionStorage.getItem('autoswe.repositoryId') || '',
    runId: sessionStorage.getItem('autoswe.runId') || '',
    projectName: sessionStorage.getItem('autoswe.projectName') || '',
    tasks: [],
    events: [],
    approvals: [],
    artifacts: [],
    currentFilter: 'ALL',
    searchQuery: '',
    pollTimer: null,
    ws: null,
    wsReconnectTimer: null,
    selectedTask: null,
    modelConfig: null,
    modelFormDirty: false,
    modelFormRevision: 0,
    modelProbeRequest: null,
    modelTestRequest: null,
    view: 'runs',
    viewEpoch: 0,
    runRequest: null,
    currentRun: null,
    socketTaskId: null,
    registeredSignature: '',
    creating: false,
    taskView: 'list',
    renderKeys: {},
    repositoryVersion: 0,
    authEpoch: 0,
    authRequest: null,
    artifactRequest: null,
    approvalDecision: null,
    cancelTarget: null,
    cancelRequests: new Map(),
  };

  function feedback(id, message, success = false) {
    const node = el(id);
    node.textContent = message;
    node.classList.toggle('hidden', !message);
    node.classList.toggle('success', success);
  }

  function repositorySignature() {
    return ['projectName', 'sourcePath', 'defaultBranch'].map(id => el(id).value.trim()).join('\n');
  }

  function stopRunUpdates() {
    window.clearTimeout(state.pollTimer);
    window.clearTimeout(state.wsReconnectTimer);
    state.runRequest?.abort();
    state.runRequest = null;
    state.socketTaskId = null;
    if (state.ws) {
      const socket = state.ws;
      state.ws = null;
      socket.onclose = socket.onmessage = socket.onopen = socket.onerror = null;
      socket.close();
    }
  }

  function setView(view, runId = '', historyMode = 'push') {
    feedback('lookupFeedback', '');
    el('runLookup').removeAttribute('aria-invalid');
    state.viewEpoch += 1;
    stopRunUpdates();
    hideRunsBrowser();
    state.view = view;
    for (const id of ['taskDrawer', 'artifactDialog', 'approvalDialog', 'cancelRunDialog']) closeDialog(el(id));
    el('dashboard').setAttribute('aria-busy', 'false');
    el('refreshRun').disabled = false;
    for (const [id, name] of [['runsBrowserSection', 'runs'], ['onboardingSection', 'new'], ['dashboard', 'run']]) {
      el(id).classList.toggle('hidden', view !== name);
    }
    document.querySelectorAll('[data-nav]').forEach(tab => {
      const active = tab.dataset.nav === (view === 'run' ? 'runs' : view);
      tab.classList.toggle('active', active);
      if (active) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
    });
    const title = { runs: 'All runs', new: 'New run', run: 'Run details' }[view];
    el('workspaceLabel').textContent = title;
    document.title = `AutoSWE · ${title}`;
    const hash = view === 'run' ? `#run/${encodeURIComponent(runId)}` : `#${view}`;
    if (historyMode !== 'none' && window.location.hash !== hash) window.history[historyMode === 'replace' ? 'replaceState' : 'pushState']({}, '', hash);
    if (view === 'runs') showRunsBrowser();
    if (historyMode === 'push') {
      el('mainContent').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
  }

  // Authentication Dialog Controls
  function openAuth() {
    cancelAuthRequest();
    feedback('authFeedback', '');
    el('adminToken').value = getToken();
    if (!el('authDialog').open) showDialog(el('authDialog'));
  }

  function closeAuth() {
    cancelAuthRequest();
    if (el('authDialog').open) closeDialog(el('authDialog'));
  }

  function cancelAuthRequest() {
    state.authRequest?.abort();
    state.authRequest = null;
    el('connectWorkspace').disabled = false;
    el('connectWorkspace').textContent = 'Connect workspace';
  }

  async function connectWorkspace(event) {
    event.preventDefault();
    if (state.authRequest) return;
    const token = el('adminToken').value.trim();
    if (!token) { feedback('authFeedback', 'Enter your admin token.'); return; }
    const request = new AbortController();
    state.authRequest = request;
    const epoch = state.authEpoch;
    const current = () => state.authRequest === request && !request.signal.aborted
      && state.authEpoch === epoch && el('authDialog').open && el('adminToken').value.trim() === token;
    el('connectWorkspace').disabled = true;
    el('connectWorkspace').textContent = 'Connecting…';
    feedback('authFeedback', 'Checking workspace access…');
    try {
      await api('/api/v1/runs?limit=1', { signal: request.signal }, token);
      if (!current()) return;
      state.authEpoch += 1;
      state.repositoryVersion += 1;
      setToken(token);
      el('authBtnText').textContent = 'Workspace access';
      closeAuth();
      showToast('Workspace connected.');
      void loadModelConfig();
      if (state.view === 'run') void loadRun(state.runId, { refresh: true });
      else if (state.view === 'runs') showRunsBrowser();
    } catch (error) {
      if (current()) feedback('authFeedback', error.message);
    } finally {
      if (state.authRequest === request) cancelAuthRequest();
    }
  }

  // Recent Runs Manager (Local Storage)
  const RECENT_RUNS_KEY = 'autoswe.recentRuns';

  function getRecentRuns() {
    try {
      const list = JSON.parse(localStorage.getItem(RECENT_RUNS_KEY));
      return Array.isArray(list) ? list.filter(item => typeof item?.id === 'string' && typeof item?.goal === 'string') : [];
    } catch (_) {
      return [];
    }
  }

  function saveRecentRun(runId, goalText) {
    if (!runId) return;
    let list = getRecentRuns();
    list = list.filter(item => item.id !== runId);
    list.unshift({
      id: runId,
      goal: (goalText || 'Workflow Execution').slice(0, 40),
      time: Date.now(),
    });
    list = list.slice(0, 6);
    try {
      localStorage.setItem(RECENT_RUNS_KEY, JSON.stringify(list));
    } catch (_) {}
    renderRecentRuns();
  }

  function renderRecentRuns() {
    const container = el('recentRunsContainer');
    const listEl = el('recentRunsList');
    if (!container || !listEl) return;

    const runs = getRecentRuns();
    if (!runs.length) {
      container.classList.add('hidden');
      return;
    }

    container.classList.remove('hidden');
    listEl.replaceChildren();

    runs.forEach(run => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'recent-run-chip';
      chip.textContent = `${run.goal} · ${run.id.slice(0, 8)}`;
      chip.addEventListener('click', () => {
        el('runLookup').value = run.id;
        void loadRun(run.id);
      });
      listEl.appendChild(chip);
    });
  }

  // Platform Readiness Healthcheck
  async function checkHealth() {
    try {
      const response = await fetch('/health/ready');
      const body = await response.json();
      const ready = response.ok && body.ready;
      const deps = body.dependencies || {};
      const unavailable = Object.entries(deps)
        .filter(([, val]) => !val)
        .map(([name]) => name);

      const coreReady = Boolean(deps.postgres && deps.redis && deps.sandbox && deps.model);

      if (ready) {
        el('healthDot').className = 'status-dot ready';
        el('healthText').textContent = 'Services connected';
      } else if (coreReady && unavailable.length === 1 && unavailable[0] === 'uams') {
        el('healthDot').className = 'status-dot ready';
        el('healthText').textContent = 'Memory service offline';
      } else {
        el('healthDot').className = 'status-dot failed';
        el('healthText').textContent = 'Some services unavailable';
        el('healthStatus').title = `Unavailable: ${unavailable.join(', ') || 'dependencies'}`;
      }
    } catch (_) {
      el('healthDot').className = 'status-dot failed';
      el('healthText').textContent = 'Services offline';
    }
  }

  // Restore Session Storage State
  function restoreIdentity() {
    el('authBtnText').textContent = getToken() ? 'Workspace access' : 'Connect workspace';
    try {
      const saved = JSON.parse(sessionStorage.getItem('autoswe.repositorySelection'));
      if (saved?.project_id && saved?.repository_id && saved?.baseline_commit) applyRepository(saved);
      else { state.projectId = ''; state.repositoryId = ''; }
    } catch (_) { state.projectId = ''; state.repositoryId = ''; }
    renderRecentRuns();
    if (state.runId) el('runLookup').value = state.runId;
  }

  function applyRepository(body) {
    state.projectId = body.project_id;
    state.repositoryId = body.repository_id;
    state.projectName = body.name;
    for (const [key, value] of Object.entries({ projectId: body.project_id, repositoryId: body.repository_id, projectName: body.name })) sessionStorage.setItem(`autoswe.${key}`, value);
    sessionStorage.setItem('autoswe.repositorySelection', JSON.stringify(body));
    el('projectName').value = body.name;
    el('sourcePath').value = body.source_path;
    el('defaultBranch').value = body.default_branch;
    el('baselineCommit').value = body.baseline_commit || '';
    state.registeredSignature = repositorySignature();
    el('projectIdentity').querySelector('.identity-text').textContent = `Connected · ${body.name} · ${body.default_branch}`;
    el('projectIdentity').classList.add('connected');
    el('startRun').disabled = state.creating || !body.baseline_commit;
    el('launchHint').textContent = body.baseline_commit ? 'Ready to start. Review the goal before continuing.' : 'A valid baseline commit is required.';
  }

  function invalidateRepository() {
    feedback('projectFeedback', '');
    state.repositoryVersion += 1;
    state.projectId = ''; state.repositoryId = ''; state.registeredSignature = '';
    sessionStorage.removeItem('autoswe.repositorySelection');
    ['projectId', 'repositoryId', 'projectName'].forEach(key => sessionStorage.removeItem(`autoswe.${key}`));
    el('projectIdentity').classList.remove('connected');
    el('projectIdentity').querySelector('.identity-text').textContent = 'Connect this repository before starting a run.';
    el('startRun').disabled = true;
    el('baselineCommit').value = '';
    el('launchHint').textContent = 'Connect a repository to continue.';
  }

  async function readDirectoryFiles(dirHandle, pathPrefix = '', maxFiles = 300) {
    const files = [];
    const skipDirs = new Set(['.git', 'node_modules', '.venv', 'venv', 'dist', 'build', '__pycache__', '.pytest_cache', '.idea', '.vscode']);
    async function scan(handle, currentPath) {
      if (files.length >= maxFiles) return;
      for await (const entry of handle.values()) {
        if (files.length >= maxFiles) break;
        if (entry.kind === 'directory') {
          if (!skipDirs.has(entry.name)) {
            await scan(entry, `${currentPath}${entry.name}/`);
          }
        } else if (entry.kind === 'file') {
          try {
            const file = await entry.getFile();
            if (file.size <= 500_000) {
              const text = await file.text();
              files.push({ path: `${currentPath}${entry.name}`, content: text });
            }
          } catch (_) {}
        }
      }
    }
    await scan(dirHandle, pathPrefix);
    return files;
  }

  async function onboardRepository(payload, version) {
    if (version !== state.repositoryVersion) return null;
    const body = await api('/api/v1/projects/onboard', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (version !== state.repositoryVersion) return null;
    applyRepository(body);
    return body;
  }

  // Directory Picker Fallback & Local Git Inspection
  async function selectDirectory(event) {
    if (event) event.preventDefault();
    if (window.showDirectoryPicker) {
      try {
        const dirHandle = await window.showDirectoryPicker({ mode: 'read' });
        if (!dirHandle) return;
        invalidateRepository();
        const version = state.repositoryVersion;
        const dirName = dirHandle.name;
        el('projectName').value = dirName;
        showToast(`Reading repository files from ${dirName}...`);

        let branch = 'main';
        try {
          const gitHandle = await dirHandle.getDirectoryHandle('.git');
          if (gitHandle) {
            const headHandle = await gitHandle.getFileHandle('HEAD');
            const headFile = await headHandle.getFile();
            const headText = (await headFile.text()).trim();
            if (headText.startsWith('ref: refs/heads/')) {
              branch = headText.replace('ref: refs/heads/', '').trim();
            }
          }
        } catch (_) {}

        const files = await readDirectoryFiles(dirHandle);
        showToast(`Auto-provisioning "${dirName}"...`);
        const onboardRes = await onboardRepository({
          name: dirName,
          folder_name: dirName,
          default_branch: branch,
          files: files,
        }, version);
        if (!onboardRes) return;

        showToast(`✓ "${dirName}" ready (${onboardRes.default_branch} · ${onboardRes.baseline_commit.slice(0, 8)})`);
        return;
      } catch (err) {
        if (err.name === 'AbortError') return;
        feedback('projectFeedback', err.message);
        return;
      }
    }

    const fallbackInput = el('dirPickerFallback');
    if (fallbackInput) {
      fallbackInput.value = '';
      fallbackInput.click();
    }
  }

  async function handleFallbackDirPicker(event) {
    const files = event.target.files;
    if (!files || !files.length) return;
    invalidateRepository();
    const version = state.repositoryVersion;
    const firstFile = files[0];
    const pathParts = (firstFile.webkitRelativePath || '').split('/');
    if (pathParts.length > 1) {
      const dirName = pathParts[0];
      el('projectName').value = dirName;
      showToast(`Uploading ${files.length} files from ${dirName}...`);

      const filePayloads = [];
      const skipDirs = new Set(['.git', 'node_modules', '.venv', 'venv', 'dist', 'build', '__pycache__', '.pytest_cache']);
      for (let i = 0; i < files.length && filePayloads.length < 300; i++) {
        const file = files[i];
        const rel = file.webkitRelativePath.replace(`${dirName}/`, '');
        const topDir = rel.split('/')[0];
        if (skipDirs.has(topDir)) continue;
        if (file.size <= 500_000) {
          try {
            const text = await file.text();
            filePayloads.push({ path: rel, content: text });
          } catch (_) {}
        }
      }

      try {
        const onboardRes = await onboardRepository({
          name: dirName,
          folder_name: dirName,
          default_branch: 'main',
          files: filePayloads,
        }, version);
        if (!onboardRes) return;
        showToast(`✓ "${dirName}" ready (${onboardRes.default_branch} · ${onboardRes.baseline_commit.slice(0, 8)})`);
      } catch (err) {
        showToast(err.message, true);
      }
    }
  }

  // Register New Project Repository
  async function registerProject(event) {
    event.preventDefault();
    const button = el('registerProjectBtn');
    if (button.disabled) return;
    button.disabled = true; button.textContent = 'Connecting…';
    const signature = repositorySignature();
    const version = ++state.repositoryVersion;
    feedback('projectFeedback', '');
    try {
      const body = await api('/api/v1/projects/onboard', {
        method: 'POST',
        body: JSON.stringify({ name: el('projectName').value.trim(), source_path: el('sourcePath').value.trim(), folder_name: el('sourcePath').value.trim(), default_branch: el('defaultBranch').value.trim() }),
      });
      if (version !== state.repositoryVersion || signature !== repositorySignature()) { feedback('projectFeedback', 'Repository fields changed. Connect again to use the updated values.'); return; }
      applyRepository(body);
      feedback('projectFeedback', 'Repository connected. Describe the work in step 2.', true);
      el('runGoal').focus();
    } catch (error) { feedback('projectFeedback', error.message); }
    finally { button.disabled = false; button.textContent = 'Connect repository'; }
  }

  async function startRun(event) {
    event.preventDefault();
    if (state.creating) return;
    if (!state.projectId || !state.repositoryId || state.registeredSignature !== repositorySignature()) {
      feedback('runFeedback', 'Connect the repository in step 1 before starting.'); return;
    }
    const commitSha = el('baselineCommit').value.trim().toLowerCase();
    const goal = el('runGoal').value.trim();
    if (!/^[a-f0-9]{40,64}$/.test(commitSha) || !goal) {
      feedback('runFeedback', 'Enter a goal and a valid full baseline commit.');
      el('baselineCommit').closest('details').open = true;
      return;
    }
    state.creating = true;
    el('startRun').disabled = true;
    el('startRun').textContent = 'Starting run…';
    feedback('runFeedback', '');
    const viewEpoch = state.viewEpoch;
    try {
      const body = await api('/api/v1/runs', { method: 'POST', body: JSON.stringify({ project_id: state.projectId, repository_id: state.repositoryId, goal, baseline_commit: commitSha }) });
      saveRecentRun(body.run_id, goal);
      showToast('Run started. Planning is underway.');
      if (viewEpoch === state.viewEpoch) await loadRun(body.run_id);
    } catch (error) { feedback('runFeedback', error.message); }
    finally {
      state.creating = false;
      el('startRun').disabled = !state.projectId;
      el('startRun').textContent = 'Start run →';
    }
  }

  // Only the current view/request may apply results. Background polling never navigates.
  async function loadRun(runId, { refresh = false, historyMode = 'push' } = {}) {
    const candidate = String(runId || '').trim();
    if (!candidate || (refresh && (state.view !== 'run' || state.runId !== candidate))) return;
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(candidate)) {
      feedback('lookupFeedback', 'Enter a valid run ID (UUID), copied from a run’s details.');
      el('runLookup').setAttribute('aria-invalid', 'true');
      el('runLookup').focus();
      return;
    }
    feedback('lookupFeedback', '');
    el('runLookup').removeAttribute('aria-invalid');
    if (refresh && document.hidden) {
      state.pollTimer = window.setTimeout(() => void loadRun(candidate, { refresh: true }), 5000);
      return;
    }
    if (!refresh) {
      const different = state.currentRun?.run_id !== candidate;
      setView('run', candidate, historyMode);
      if (different) {
        state.currentRun = null;
        updateCancelControl();
        state.tasks = []; state.approvals = []; state.artifacts = []; state.events = [];
        state.renderKeys = {};
        el('runGoalTitle').textContent = 'Loading run…';
        el('runStatusBadge').textContent = 'Loading';
        el('runStatusBadge').className = 'status-badge';
        el('runProjectName').textContent = '—';
        el('runIdText').textContent = candidate;
        for (const id of ['runState', 'stateDuration', 'planRevision', 'taskSummary', 'tokenTotal', 'tokenDetail', 'modelCost']) el(id).textContent = '—';
        el('dagProgressBar').style.width = '0%';
        for (const id of ['taskList', 'taskDag', 'approvalList', 'artifactList', 'eventList']) el(id).replaceChildren();
        el('runFailureBanner')?.remove();
        el('approvalsTabCount').textContent = ''; el('artifactsTabCount').textContent = '';
        selectPanel('tasks');
        el('taskList').innerHTML = '<div class="empty-state">Loading tasks…</div>';
      }
    }
    state.runId = candidate;
    el('runLookup').value = candidate;
    window.clearTimeout(state.pollTimer);
    state.runRequest?.abort();
    const request = new AbortController();
    state.runRequest = request;
    const current = () => state.runRequest === request && !request.signal.aborted && state.view === 'run' && state.runId === candidate;
    el('refreshRun').disabled = true;
    el('dashboard').setAttribute('aria-busy', 'true');
    feedback('runFeedbackBanner', '');
    try {
      const run = await api(`/api/v1/runs/${encodeURIComponent(candidate)}`, { signal: request.signal });
      if (!current()) return;
      state.currentRun = run;
      sessionStorage.setItem('autoswe.runId', candidate);
      if (!refresh) saveRecentRun(candidate, run.goal);
      renderRun(run);
      const resources = ['tasks', 'approvals', 'artifacts', 'events'];
      const results = await Promise.allSettled(resources.map(resource => api(`/api/v1/runs/${encodeURIComponent(candidate)}/${resource}${resource === 'events' ? '?limit=500' : ''}`, { signal: request.signal })));
      if (!current()) return;
      const unavailable = [];
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') state[resources[index]] = resources[index] === 'events'
          ? mergeEvents(state.events, result.value || []) : result.value || [];
        else unavailable.push(resources[index]);
      });
      renderRun(run);
      if (unavailable.length) feedback('runFeedbackBanner', `Could not update ${unavailable.join(', ')}. Previously loaded data is kept. Use Refresh to retry.`);
      setupWebSocket(run.project_id, candidate);
      if (!terminalStates.has(run.state)) state.pollTimer = window.setTimeout(() => void loadRun(candidate, { refresh: true }), 5000);
    } catch (error) {
      if (!current()) return;
      feedback('runFeedbackBanner', `${error.message} Use Refresh to try again, or return to All runs.`);
      el('streamStatusText').textContent = 'Update paused';
      el('liveStreamBadge').classList.remove('live');
      if (!state.currentRun) {
        el('runGoalTitle').textContent = 'Run couldn’t be loaded';
        el('runStatusBadge').textContent = 'Unavailable';
        el('taskList').innerHTML = '<div class="empty-state">Run data is unavailable.</div>';
      }
      if (getToken()) state.pollTimer = window.setTimeout(() => void loadRun(candidate, { refresh: true }), 8000);
    } finally {
      if (current()) {
        el('refreshRun').disabled = false;
        el('dashboard').setAttribute('aria-busy', 'false');
        state.runRequest = null;
      }
    }
  }

  let wsRetryDelay = 1000;
  function eventKey(event) { return String(event.event_id || event.id || `${event.created_at}:${event.event_type}:${JSON.stringify(event.payload)}`); }
  function mergeEvents(previous, incoming) {
    const records = new Map([...previous, ...incoming].map(event => [eventKey(event), event]));
    return [...records.values()].sort((a, b) => new Date(b.created_at) - new Date(a.created_at) || eventKey(b).localeCompare(eventKey(a))).slice(0, 500);
  }
  function setupWebSocket(projectId, runId) {
    const task = state.tasks.find(t => t.state === 'RUNNING' || t.state === 'LEASED');
    if (task?.task_id === state.socketTaskId && state.ws) return;
    window.clearTimeout(state.wsReconnectTimer);
    if (state.ws) { state.ws.onclose = state.ws.onmessage = state.ws.onopen = state.ws.onerror = null; state.ws.close(); }
    state.ws = null; state.socketTaskId = null;
    if (!task || terminalStates.has(state.currentRun?.state)) {
      el('liveStreamBadge').classList.remove('live');
      el('streamStatusText').textContent = terminalStates.has(state.currentRun?.state) ? 'Run finished' : 'Auto-updating';
      return;
    }
    state.socketTaskId = task.task_id;
    const valid = () => state.view === 'run' && state.runId === runId && state.socketTaskId === task.task_id;
    state.ws = connectTaskSocket(projectId, task.task_id, {
      onEvent(payload) {
        if (!valid()) return;
        if (state.events.some(event => eventKey(event) === eventKey(payload))) return;
        state.events = mergeEvents(state.events, [payload]);
        renderEvents(state.events);
      },
      onState(status) {
        if (!valid()) return;
        el('liveStreamBadge').classList.toggle('live', status === 'open');
        if (status === 'open') { wsRetryDelay = 1000; el('streamStatusText').textContent = 'Live updates'; }
        else el('streamStatusText').textContent = 'Auto-updating';
        if (status === 'close') {
          state.ws = null;
          state.wsReconnectTimer = window.setTimeout(() => { if (valid()) setupWebSocket(projectId, runId); }, wsRetryDelay);
          wsRetryDelay = Math.min(wsRetryDelay * 2, 8000);
        }
      },
    });
  }

  // Render Dashboard
  function renderRun(run) {
    updateCancelControl();
    el('runGoalTitle').textContent = run.goal;
    el('runIdText').textContent = run.run_id;
    el('runProjectName').textContent = run.project_name || run.project_id.slice(0, 8);
    
    // Status Badge
    const statusBadge = el('runStatusBadge');
    statusBadge.textContent = humanState(run.state);
    statusBadge.className = `status-badge ${run.state.toLowerCase()}`;

    // Metrics
    el('runState').textContent = humanState(run.state);
    el('stateDuration').textContent = `${formatDuration(run.state_duration_seconds)} in current state`;
    el('planRevision').textContent = run.active_plan_revision == null
      ? (terminalStates.has(run.state) ? 'No plan' : run.state === 'PLANNING' ? 'Planning' : 'Awaiting plan')
      : `r${run.active_plan_revision}`;
    const counts = summarizeCounts(run.task_counts);
    el('taskSummary').textContent = counts.total ? `${counts.done} / ${counts.total} complete` : 'No tasks created';

    // Calculate Task Progress Fill
    const taskCounts = run.task_counts || {};
    const totalTasks = Object.values(taskCounts).reduce((a, b) => a + b, 0);
    const completedTasks = taskCounts.COMPLETED || 0;
    const pct = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
    const progressFill = el('dagProgressBar');
    if (progressFill) progressFill.style.width = `${pct}%`;

    // Failure banner for runs that never produced a DAG — the UI previously showed an empty canvas with no explanation.
    const existingBanner = el('runFailureBanner');
    if (existingBanner) existingBanner.remove();
    if (run.state === 'FAILED' && totalTasks === 0) {
      const dagPanel = document.querySelector('.dag-canvas-panel');
      if (dagPanel) {
        const banner = document.createElement('div');
        banner.id = 'runFailureBanner';
        banner.className = 'failure-banner';
        banner.innerHTML = `
          <div class="failure-icon">✕</div>
          <div class="failure-body">
            <h4>Planning failed — no tasks were created</h4>
            <p>This run stopped before a task plan was created. Review Activity for recorded errors and check model settings before starting again.</p>
          </div>
          <button class="button primary" id="retryRunBtn" type="button">Use this goal</button>`;
        dagPanel.prepend(banner);
        banner.querySelector('#retryRunBtn')?.addEventListener('click', () => {
          el('runGoal').value = run.goal;
          invalidateRepository();
          el('projectName').value = run.project_name || '';
          el('sourcePath').value = '';
          showLaunchpad();
          window.scrollTo({ top: 0, behavior: 'smooth' });
          showToast('Goal restored. Connect the correct repository before starting.');
        });
      }
    }

    el('tokenTotal').textContent = ((run.model_input_tokens || 0) + (run.model_output_tokens || 0)).toLocaleString();
    el('tokenDetail').textContent = `${(run.model_input_tokens || 0).toLocaleString()} input · ${(run.model_output_tokens || 0).toLocaleString()} output`;
    el('modelCost').textContent = `$${Number(run.model_cost_usd || 0).toFixed(4)}`;
    const groups = [
      ['tasks', [run.state, state.tasks], () => { renderTaskList(state.tasks); if (state.taskView === 'graph') renderDAG(state.tasks); }],
      ['approvals', state.approvals, () => renderApprovals(state.approvals)],
      ['artifacts', state.artifacts, () => renderArtifacts(state.artifacts, run.project_id)],
    ];
    groups.forEach(([key, value, render]) => {
      const signature = JSON.stringify(value);
      if (state.renderKeys[key] !== signature) { render(); state.renderKeys[key] = signature; }
    });
    el('approvalsTabCount').textContent = state.approvals.filter(a => a.status === 'PENDING').length || '';
    el('artifactsTabCount').textContent = state.artifacts.length || '';
    renderEvents(state.events);
  }

  function emptyTasks() {
    return state.currentRun && !terminalStates.has(state.currentRun.state)
      ? 'The task plan is being prepared. Tasks will appear here when ready.'
      : 'No tasks were created for this run.';
  }

  function renderTaskList(tasks) {
    const root = el('taskList');
    if (!tasks.length) { root.innerHTML = `<div class="empty-state">${emptyTasks()}</div>`; return; }
    root.querySelector('.empty-state')?.remove();
    const ids = new Set(tasks.map(t => t.task_id));
    for (const row of [...root.children]) if (!ids.has(row.dataset.taskId)) row.remove();
    tasks.forEach((task, index) => {
      let button = [...root.children].find(row => row.dataset.taskId === task.task_id);
      if (!button) {
        button = document.createElement('button'); button.type = 'button'; button.className = 'task-list-row'; button.dataset.taskId = task.task_id;
        button.addEventListener('click', () => { const latest = state.tasks.find(t => t.task_id === button.dataset.taskId); if (latest) openTaskDrawer(latest); });
      }
      const html = `<span class="task-number">${String(index + 1).padStart(2, '0')}</span><span><span class="task-list-name">${escapeHtml(task.title)}</span><span class="task-list-meta">${task.dependencies?.length ? `${task.dependencies.length} prerequisite task(s)` : 'No prerequisites'}</span></span><span class="state-pill tone-${stateTone(task.state)}"><span class="pill-dot"></span>${humanState(task.state)}</span><span class="task-list-type">${humanState(task.task_type)}</span><span aria-hidden="true">›</span>`;
      if (button.innerHTML !== html) button.innerHTML = html;
      if (root.children[index] !== button) root.insertBefore(button, root.children[index] || null);
    });
  }

  function selectPanel(name) {
    document.querySelectorAll('[data-panel]').forEach(tab => {
      const active = tab.dataset.panel === name;
      tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1;
      el(`panel-${tab.dataset.panel}`).classList.toggle('hidden', !active);
    });
    if (name === 'tasks' && state.taskView === 'graph') renderDAG(state.tasks);
  }

  function updateCancelControl() {
    const active = state.view === 'run' && state.currentRun && !terminalStates.has(state.currentRun.state);
    el('cancelRun').classList.toggle('hidden', !active);
    el('cancelRun').disabled = state.cancelRequests.has(state.runId);
    if (!active && el('cancelRunDialog').open) closeDialog(el('cancelRunDialog'));
  }

  function openCancelDialog() {
    if (state.view !== 'run' || !state.currentRun || terminalStates.has(state.currentRun.state)
      || state.cancelRequests.has(state.runId)) return;
    state.cancelTarget = { runId: state.runId, epoch: state.authEpoch };
    el('cancelRunDescription').textContent = `Active work will stop for “${state.currentRun.goal}” (${state.runId}). This cannot be undone.`;
    feedback('cancelRunFeedback', '');
    el('confirmCancelRun').disabled = false;
    el('confirmCancelRun').textContent = 'Cancel run';
    showDialog(el('cancelRunDialog'), el('keepRun'));
  }

  async function submitCancellation(event) {
    event.preventDefault();
    const target = state.cancelTarget;
    if (!target || state.cancelRequests.has(target.runId) || target.epoch !== state.authEpoch
      || state.view !== 'run' || state.runId !== target.runId || terminalStates.has(state.currentRun?.state)) return;
    state.cancelRequests.set(target.runId, target);
    el('confirmCancelRun').disabled = true;
    el('confirmCancelRun').textContent = 'Cancelling…';
    feedback('cancelRunFeedback', 'Requesting cancellation. Closing this dialog will not undo the request.');
    updateCancelControl();
    try {
      await api(`/api/v1/runs/${encodeURIComponent(target.runId)}/cancel`, { method: 'POST' });
      if (target.epoch !== state.authEpoch) return;
      if (state.cancelTarget === target) closeDialog(el('cancelRunDialog'));
      if (state.view === 'run' && state.runId === target.runId) {
        showToast('Cancellation requested — workers will wind down.');
        void loadRun(target.runId, { refresh: true });
      }
    } catch (error) {
      if (target.epoch !== state.authEpoch) return;
      if (state.cancelTarget === target) feedback('cancelRunFeedback', error.message);
      else if (state.view === 'run' && state.runId === target.runId) showToast(error.message, true);
    } finally {
      if (state.cancelRequests.get(target.runId) === target) state.cancelRequests.delete(target.runId);
      if (state.cancelTarget === target) {
        el('confirmCancelRun').disabled = false;
        el('confirmCancelRun').textContent = 'Cancel run';
      }
      updateCancelControl();
    }
  }

  // Topological DAG Layout & Stage Grouping Engine
  function isRunningState(state) { return state === 'RUNNING' || state === 'LEASED'; }

  function renderDAG(tasks) {
    const root = el('taskDag');
    const svg = el('dagSvgConnections');
    root.replaceChildren();
    svg.replaceChildren();

    if (!tasks || !tasks.length) {
      root.innerHTML = `<div class="empty-state">${emptyTasks()}</div>`;
      return;
    }

    // Compute topological ranks for each task
    const taskMap = new Map(tasks.map(t => [t.task_id, t]));
    const ranks = new Map();

    function getRank(taskId, visited = new Set()) {
      if (ranks.has(taskId)) return ranks.get(taskId);
      if (visited.has(taskId)) return 0;
      visited.add(taskId);

      const task = taskMap.get(taskId);
      if (!task || !task.dependencies || !task.dependencies.length) {
        ranks.set(taskId, 0);
        return 0;
      }

      let maxParentRank = -1;
      for (const parentId of task.dependencies) {
        maxParentRank = Math.max(maxParentRank, getRank(parentId, new Set(visited)));
      }
      const rank = maxParentRank + 1;
      ranks.set(taskId, rank);
      return rank;
    }

    tasks.forEach(t => getRank(t.task_id));

    // Group tasks by rank
    const maxRank = Math.max(...Array.from(ranks.values()), 0);
    const columns = Array.from({ length: maxRank + 1 }, () => []);

    tasks.forEach(t => {
      const rank = ranks.get(t.task_id) || 0;
      columns[rank].push(t);
    });

    // Render Columns & Task Cards
    columns.forEach((columnTasks, levelIdx) => {
      const colEl = document.createElement('div');
      colEl.className = 'dag-column';

      const colHeader = document.createElement('div');
      colHeader.className = 'dag-column-header';
      
      const titleSpan = document.createElement('span');
      titleSpan.textContent = `Stage ${levelIdx + 1}`;
      
      const countBadge = document.createElement('span');
      countBadge.className = 'brand-version-pill';
      const completedCount = columnTasks.filter(t => t.state === 'COMPLETED').length;
      countBadge.textContent = `${completedCount}/${columnTasks.length}`;

      colHeader.append(titleSpan, countBadge);
      colEl.append(colHeader);

      columnTasks.forEach(task => {
        const node = document.createElement('button');
        node.type = 'button';
        node.className = `task-node ${task.state.toLowerCase()}${isRunningState(task.state) ? ' is-running' : ''}`;
        node.id = `node-${task.task_id}`;
        node.dataset.taskId = task.task_id;

        const header = document.createElement('div');
        header.className = 'task-node-header';
        
        const typeTag = document.createElement('span');
        typeTag.className = 'task-type-tag';
        typeTag.textContent = humanState(task.task_type);

        const statusPill = document.createElement('span');
        statusPill.className = `status-badge ${task.state.toLowerCase()}`;
        statusPill.textContent = humanState(task.state);
        header.append(typeTag, statusPill);

        const title = document.createElement('h4');
        title.className = 'task-node-title';
        title.textContent = task.title;

        const meta = document.createElement('div');
        meta.className = 'task-node-meta';
        const depsCount = task.dependencies ? task.dependencies.length : 0;
        meta.innerHTML = `<span>${escapeHtml(task.assigned_capability)}</span><span>${depsCount ? `${depsCount} dep${depsCount > 1 ? 's' : ''}` : 'Root'}</span>`;

        node.append(header, title, meta);
        node.addEventListener('click', () => openTaskDrawer(task));
        colEl.append(node);
      });

      root.append(colEl);
    });

    window.requestAnimationFrame(() => drawDAGConnectors(tasks));
  }

  // Draw Smooth Bezier Curves between DAG Nodes
  function drawDAGConnectors(tasks) {
    const svg = el('dagSvgConnections');
    const viewport = el('dagViewport');
    if (!svg || !viewport) return;

    svg.replaceChildren();

    // Re-insert defs gradient
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = `
      <linearGradient id="activeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#06b6d4" />
        <stop offset="100%" stop-color="#38bdf8" />
      </linearGradient>
    `;
    svg.appendChild(defs);

    const viewportRect = viewport.getBoundingClientRect();
    svg.setAttribute('width', viewport.scrollWidth);
    svg.setAttribute('height', viewport.scrollHeight);

    tasks.forEach(task => {
      const childNode = el(`node-${task.task_id}`);
      if (!childNode) return;
      const childRect = childNode.getBoundingClientRect();

      (task.dependencies || []).forEach(parentId => {
        const parentNode = el(`node-${parentId}`);
        if (!parentNode) return;
        const parentRect = parentNode.getBoundingClientRect();

        const startX = parentRect.right - viewportRect.left + viewport.scrollLeft;
        const startY = parentRect.top + (parentRect.height / 2) - viewportRect.top + viewport.scrollTop;
        const endX = childRect.left - viewportRect.left + viewport.scrollLeft;
        const endY = childRect.top + (childRect.height / 2) - viewportRect.top + viewport.scrollTop;

        const dx = Math.max(Math.abs(endX - startX) * 0.5, 30);
        const pathData = `M ${startX} ${startY} C ${startX + dx} ${startY}, ${endX - dx} ${endY}, ${endX} ${endY}`;

        const isRunning = task.state === 'RUNNING' || task.state === 'LEASED';

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', pathData);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', isRunning ? 'url(#activeGrad)' : '#b8c7ce');
        path.setAttribute('stroke-width', isRunning ? '2.4' : '1.6');
        path.classList.add(isRunning ? 'live-edge' : 'idle-edge');
        svg.append(path);
      });
    });
  }

  // Slide-Over Task Detail Drawer
  function openTaskDrawer(task) {
    state.selectedTask = task;
    el('drawerTaskType').textContent = task.task_type;
    el('drawerTaskTitle').textContent = task.title;
    el('drawerTaskId').textContent = task.task_id;
    
    const stateEl = el('drawerTaskState');
    stateEl.textContent = humanState(task.state);
    stateEl.className = `state-pill ${task.state.toLowerCase()}`;

    el('drawerTaskCapability').textContent = task.assigned_capability;
    el('drawerTaskPriority').textContent = `Priority ${task.priority}`;
    el('drawerTaskDeps').textContent = task.dependencies && task.dependencies.length ? task.dependencies.join(', ') : 'None (Root)';
    el('drawerTaskRevision').textContent = `r${task.plan_revision}`;

    el('drawerTaskGoal').textContent = task.description || task.goal || task.title;

    // Agent reasoning feed (handoff summaries) for this task.
    void loadTaskIntel(state.currentRun?.project_id, task.task_id, el('drawerTaskIntel'));

    // Filter events for this task
    const taskEvents = state.events.filter(e => e.payload && e.payload.task_id === task.task_id);
    const eventsContainer = el('drawerTaskEvents');
    eventsContainer.replaceChildren();

    if (!taskEvents.length) {
      eventsContainer.innerHTML = '<p class="empty-text">No direct activity recorded for this task yet.</p>';
    } else {
      taskEvents.forEach(evt => {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.innerHTML = `
          <span class="timeline-time">${new Date(evt.created_at).toLocaleTimeString()}</span>
          <span class="timeline-event-name">${escapeHtml(evt.event_type)}</span>
          <span class="timeline-data">${escapeHtml(JSON.stringify(evt.payload))}</span>
        `;
        eventsContainer.append(item);
      });
    }

    if (!el('taskDrawer').open) showDialog(el('taskDrawer'));
  }

  // Render Governed Approval Queue
  function renderApprovals(approvals) {
    const root = el('approvalList');
    const badge = el('pendingApprovalsCount');
    root.replaceChildren();

    const pending = (approvals || []).filter(a => a.status === 'PENDING');
    if (pending.length > 0) {
      badge.textContent = pending.length;
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }

    if (!approvals || !approvals.length) {
      root.innerHTML = '<div class="empty-state">No pending approvals.</div>';
      return;
    }

    approvals.forEach(approval => {
      const card = document.createElement('div');
      card.className = 'approval-item';

      const header = document.createElement('div');
      header.className = 'approval-header';

      const toolTitle = document.createElement('span');
      toolTitle.className = 'approval-tool';
      toolTitle.textContent = approval.tool_name;

      const riskBadge = document.createElement('span');
      const risk = (approval.risk_level || 'HIGH').toLowerCase();
      riskBadge.className = `risk-badge ${risk}`;
      riskBadge.textContent = `${approval.status} · ${risk}`;
      header.append(toolTitle, riskBadge);

      const hash = document.createElement('div');
      hash.className = 'approval-hash';
      hash.textContent = `Call Hash: ${approval.call_hash.slice(0, 16)}...`;

      const expiry = document.createElement('div');
      expiry.className = 'approval-hash';
      expiry.textContent = `Expires: ${new Date(approval.expires_at).toLocaleTimeString()}`;

      card.append(header, hash, expiry);

      if (approval.status === 'PENDING') {
        const actions = document.createElement('div');
        actions.className = 'approval-actions-row';

        const rejectBtn = document.createElement('button');
        rejectBtn.className = 'button danger';
        rejectBtn.textContent = 'Reject';
        rejectBtn.onclick = () => decideApproval(approval, false);

        const approveBtn = document.createElement('button');
        approveBtn.className = 'button primary';
        approveBtn.textContent = 'Approve Exact Call';
        approveBtn.onclick = () => decideApproval(approval, true);

        actions.append(rejectBtn, approveBtn);
        card.append(actions);
      }

      root.append(card);
    });
  }

  // Operator Approval Decision
  function decideApproval(approval, approved) {
    state.approvalDecision = { approval, approved, runId: state.runId, viewEpoch: state.viewEpoch };
    el('approvalTitle').textContent = approved ? `Approve ${approval.tool_name}?` : `Reject ${approval.tool_name}?`;
    el('approvalDescription').textContent = `This decision applies only to the exact call ${approval.call_hash.slice(0, 16)}… and is recorded in the audit log.`;
    el('confirmApproval').textContent = approved ? 'Approve exact call' : 'Reject call';
    el('confirmApproval').disabled = false;
    feedback('approvalFeedback', '');
    showDialog(el('approvalDialog'));
  }

  async function submitApproval(event) {
    event.preventDefault();
    const decision = state.approvalDecision;
    const approver = el('approvalOperator').value.trim();
    if (!decision || !approver || el('confirmApproval').disabled) return;
    const { approval, approved, runId, viewEpoch } = decision;
    el('confirmApproval').disabled = true;
    try {
      await api(`/api/v1/approvals/${encodeURIComponent(approval.approval_id)}/decision`, {
        method: 'POST', body: JSON.stringify({ approved, approver, expected_call_hash: approval.call_hash }),
      });
      if (state.approvalDecision === decision) closeDialog(el('approvalDialog'));
      showToast(approved ? 'Tool call approved.' : 'Tool call rejected.');
      if (state.view === 'run' && state.runId === runId && state.viewEpoch === viewEpoch) await loadRun(runId, { refresh: true });
    } catch (error) {
      if (state.approvalDecision === decision) feedback('approvalFeedback', error.message);
    } finally {
      if (state.approvalDecision === decision) el('confirmApproval').disabled = false;
    }
  }

  // Render Verified Artifacts
  function renderArtifacts(artifacts, projectId) {
    const root = el('artifactList');
    root.replaceChildren();

    if (!artifacts || !artifacts.length) {
      root.innerHTML = '<div class="empty-state">No artifacts generated yet.</div>';
      return;
    }

    artifacts.forEach(art => {
      const item = document.createElement('div');
      item.className = 'artifact-item';

      const meta = document.createElement('div');
      meta.className = 'artifact-meta';

      const name = document.createElement('span');
      name.className = 'artifact-name';
      name.textContent = art.media_type;

      const details = document.createElement('span');
      details.className = 'artifact-details';
      details.textContent = `${formatBytes(art.size_bytes)} · sha256:${art.sha256.slice(0, 10)}...`;

      meta.append(name, details);

      const previewBtn = document.createElement('button');
      previewBtn.className = 'button ghost icon-btn';
      previewBtn.title = 'Preview Artifact';
      previewBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
      previewBtn.onclick = (e) => {
        e.stopPropagation();
        previewArtifact(projectId, art);
      };

      item.append(meta, previewBtn);
      item.tabIndex = 0; item.setAttribute('role', 'button');
      item.setAttribute('aria-label', `Preview ${art.media_type} file`);
      item.onclick = () => previewArtifact(projectId, art);
      item.addEventListener('keydown', event => { if (event.target === item && ['Enter', ' '].includes(event.key)) { event.preventDefault(); void previewArtifact(projectId, art); } });
      root.append(item);
    });
  }

  // Artifact Preview & Diff Formatting
  async function previewArtifact(projectId, artifact) {
    el('modalArtifactType').textContent = artifact.media_type;
    el('modalArtifactTitle').textContent = `Artifact ${artifact.artifact_id.slice(0, 8)}`;
    el('modalArtifactMeta').textContent = `SHA-256: ${artifact.sha256} • Size: ${formatBytes(artifact.size_bytes)}`;
    state.artifactRequest?.abort();
    const request = new AbortController();
    state.artifactRequest = request;
    const current = () => state.artifactRequest === request && !request.signal.aborted && el('artifactDialog').open;
    el('artifactPreviewCode').textContent = 'Loading file preview…';
    el('downloadArtifactBtn').onclick = () => downloadArtifact(projectId, artifact);
    if (!el('artifactDialog').open) showDialog(el('artifactDialog'));

    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${getToken()}` }, signal: request.signal,
      });
      if (!response.ok) throw new Error(`Fetch failed (${response.status})`);
      const text = await response.text();
      if (!current()) return;
      
      // Syntax-highlight diff additions / deletions
      if (artifact.media_type === 'DIFF' || text.startsWith('diff --git') || text.includes('@@ -')) {
        const lines = text.split('\n');
        const formatted = lines.map(line => {
          if (line.startsWith('+') && !line.startsWith('+++')) {
            return `<span class="diff-line-add">${escapeHtml(line)}</span>`;
          } else if (line.startsWith('-') && !line.startsWith('---')) {
            return `<span class="diff-line-del">${escapeHtml(line)}</span>`;
          }
          return escapeHtml(line);
        }).join('\n');
        el('artifactPreviewCode').innerHTML = formatted || '(Empty content)';
      } else {
        el('artifactPreviewCode').textContent = text || '(Empty content)';
      }
    } catch (err) {
      if (!current()) return;
      el('artifactPreviewCode').textContent = `Failed to preview artifact: ${err.message}`;
    }
  }

  async function downloadArtifact(projectId, artifact) {
    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!response.ok) throw new Error(`Download failed (${response.status})`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `artifact-${artifact.artifact_id.slice(0, 8)}.txt`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  // Render Immutable Audit Events Timeline with Live Search (virtualized)
  function renderEvents(events) {
    const query = (state.searchQuery || '').toLowerCase().trim();
    const filtered = (events || []).filter((e) => {
      if (state.currentFilter === 'TASK' && !e.event_type.startsWith('task.')) return false;
      if (state.currentFilter === 'TOOL' && !e.event_type.startsWith('tool.')) return false;
      if (state.currentFilter === 'APPROVAL' && !e.event_type.startsWith('approval.')) return false;
      if (query) {
        const payloadStr = JSON.stringify(e.payload || {}).toLowerCase();
        const typeStr = e.event_type.toLowerCase();
        if (!typeStr.includes(query) && !payloadStr.includes(query)) return false;
      }
      return true;
    });

    const signature = JSON.stringify([state.currentFilter, query, filtered]);
    if (state.renderKeys.events === signature) return;
    state.renderKeys.events = signature;
    const root = el('eventList');
    const expanded = new Set([...root.querySelectorAll('details[open]')].map(node => node.dataset.eventKey));
    root.replaceChildren();
    if (!filtered.length) { root.innerHTML = '<li class="timeline-empty">No matching activity.</li>'; return; }
    filtered.forEach((event, index) => {
      const item = document.createElement('li'); item.className = 'timeline-item';
      const details = document.createElement('details');
      details.dataset.eventKey = eventKey(event);
      details.open = expanded.has(details.dataset.eventKey);
      const summary = document.createElement('summary');
      const time = document.createElement('time'); time.className = 'timeline-time'; time.textContent = new Date(event.created_at).toLocaleTimeString();
      const name = document.createElement('span'); name.className = 'timeline-event-name'; name.textContent = event.event_type;
      summary.append(time, name);
      const data = document.createElement('pre'); data.textContent = JSON.stringify(event.payload || {}, null, 2);
      details.append(summary, data); item.append(details); root.append(item);
    });
  }

  // Model settings and discovery
  function modelDiscoveryStatus(message, success = false) {
    feedback('modelDiscoveryStatus', message, success);
  }

  function cancelModelTest() {
    state.modelTestRequest?.abort();
    state.modelTestRequest = null;
    el('testModelBtn').disabled = false;
    el('testModelBtn').textContent = 'Test connection';
    el('modelTestCard').classList.add('hidden');
  }

  function cancelModelRequests() {
    state.modelProbeRequest?.abort();
    state.modelProbeRequest = null;
    el('probeModelsBtn').disabled = false;
    el('probeModelsBtn').textContent = 'Discover models';
    cancelModelTest();
  }

  function modelSelectionChanged() {
    state.modelFormDirty = true;
    state.modelFormRevision += 1;
    cancelModelTest();
  }

  function syncProviderChips() {
    const endpoint = el('modelBaseUrl').value.trim();
    let provider = state.modelConfig?.base_url === endpoint ? state.modelConfig.provider_name?.toLowerCase() : '';
    if (/11434|ollama/i.test(endpoint)) provider = 'ollama';
    document.querySelectorAll('.provider-chip').forEach(chip => {
      const active = chip.dataset.provider === provider || (!!chip.dataset.url && chip.dataset.url === endpoint);
      chip.classList.toggle('active', active);
      chip.setAttribute('aria-pressed', String(active));
    });
  }

  function fillModelSettings() {
    const config = state.modelConfig;
    el('modelBaseUrl').value = config?.base_url || '';
    el('primaryModelInput').value = config?.primary_model || '';
    el('fallbackModelsInput').value = (config?.fallback_models || []).join(', ');
    el('modelTimeoutInput').value = config?.timeout_seconds || 300;
    el('modelTemperatureInput').value = config?.temperature || 0;
    el('apiKeyStatusHint').textContent = config?.has_api_key
      ? 'A key is saved for this endpoint. Leave blank to keep it, or enter a replacement.'
      : 'Leave blank for local Ollama. Changing the endpoint clears the old key.';
    populateModelDropdowns([], config?.primary_model || '');
    syncProviderChips();
    modelDiscoveryStatus('Choose a provider or enter a base URL to discover models.');
    if (getToken() && config?.base_url) void probeModels();
  }

  function openModelStudio() {
    cancelModelRequests();
    state.modelFormDirty = false;
    state.modelFormRevision += 1;
    // API tokens are not login passwords; never reuse browser-autofilled values.
    el('modelApiKey').value = '';
    el('modelApiKey').type = 'password';
    if (!el('modelStudioDialog').open) showDialog(el('modelStudioDialog'));
    fillModelSettings();
    if (!getToken()) modelDiscoveryStatus('Connect your workspace to discover models.');
  }

  function closeModelStudio() {
    state.modelFormRevision += 1;
    cancelModelRequests();
    if (el('modelStudioDialog').open) closeDialog(el('modelStudioDialog'));
  }

  function updateModelBadge(config) {
    if (!config) return;
    const model = config.primary_model || 'Unknown';
    const provider = config.provider_name || 'LLM';
    
    const topbarText = el('topbarModelName');
    if (topbarText) topbarText.textContent = model;

    const providerLabel = el('launchpadProviderLabel');
    if (providerLabel) {
      providerLabel.textContent = `${provider} (${config.base_url})`;
    }
  }

  function populateModelDropdowns(models, selected) {
    const available = [...new Set([selected, ...(Array.isArray(models) ? models : [])].filter(Boolean))];
    const launch = el('launchpadModelSelect');
    launch.replaceChildren();
    const current = document.createElement('option');
    current.textContent = state.modelConfig?.primary_model || 'Model unavailable — check settings';
    current.value = state.modelConfig?.primary_model || '';
    launch.append(current);
    const primary = el('primaryModelSelect'); primary.replaceChildren();
    available.forEach(model => { const option = document.createElement('option'); option.value = model; option.textContent = model; primary.append(option); });
    const custom = document.createElement('option'); custom.value = '__custom__'; custom.textContent = 'Enter a custom model'; primary.append(custom);
    primary.value = selected || '__custom__';
    if (selected) el('primaryModelInput').value = selected;
    const chips = el('discoveredModelsChips'); chips.replaceChildren();
    [...new Set(models)].forEach(model => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'preset-chip'; button.textContent = model;
      button.addEventListener('click', () => {
        modelSelectionChanged();
        el('primaryModelInput').value = model; primary.value = model;
      }); chips.append(button);
    });
  }

  async function loadModelConfig() {
    const authEpoch = state.authEpoch;
    try {
      const config = await api('/api/v1/models/config');
      if (authEpoch !== state.authEpoch || !getToken()) return;
      state.modelConfig = config;
      updateModelBadge(config);
      if (el('modelStudioDialog').open) {
        if (!state.modelFormDirty) fillModelSettings();
      } else populateModelDropdowns([], config.primary_model);
    } catch (_) {
      if (authEpoch !== state.authEpoch || !getToken()) return;
      const topbarText = el('topbarModelName');
      if (topbarText) topbarText.textContent = 'Settings unavailable';
      populateModelDropdowns([], '');
    }
  }

  async function saveModelConfig(event) {
    event.preventDefault();
    const button = el('modelStudioForm').querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    const authEpoch = state.authEpoch;
    const formRevision = state.modelFormRevision;
    const payload = {
      base_url: el('modelBaseUrl').value.trim(),
      api_key: el('modelApiKey').value.trim(),
      primary_model: el('primaryModelInput').value.trim(),
      fallback_models: el('fallbackModelsInput').value
        .split(',')
        .map(s => s.trim())
        .filter(Boolean),
      timeout_seconds: parseFloat(el('modelTimeoutInput').value) || 300,
      temperature: parseFloat(el('modelTemperatureInput').value) || 0.0,
    };

    try {
      showToast('Saving model configuration...');
      const updated = await api('/api/v1/models/config', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (authEpoch !== state.authEpoch || !getToken()) return;
      state.modelConfig = updated;
      updateModelBadge(updated);
      if (formRevision === state.modelFormRevision && el('modelStudioDialog').open) {
        populateModelDropdowns([], updated.primary_model);
        closeModelStudio();
      }
      showToast(`✓ Active Model: ${updated.primary_model} (${updated.provider_name})`);
    } catch (err) {
      if (authEpoch === state.authEpoch && getToken()) showToast(err.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function probeModels() {
    const baseUrl = el('modelBaseUrl').value.trim();
    if (!baseUrl || !getToken() || !el('modelStudioDialog').open) return;
    state.modelProbeRequest?.abort();
    const request = new AbortController();
    state.modelProbeRequest = request;
    const authEpoch = state.authEpoch;
    const apiKey = el('modelApiKey').value.trim();
    const isCurrent = () => state.modelProbeRequest === request && authEpoch === state.authEpoch
      && getToken() && el('modelStudioDialog').open && el('modelBaseUrl').value.trim() === baseUrl
      && el('modelApiKey').value.trim() === apiKey;
    const probeBtn = el('probeModelsBtn');
    probeBtn.disabled = true;
    probeBtn.textContent = 'Discovering…';
    populateModelDropdowns([], el('primaryModelInput').value.trim());
    modelDiscoveryStatus('Looking for models at this endpoint…', true);
    try {
      const res = await api('/api/v1/models/probe', {
        method: 'POST', signal: request.signal,
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
      });
      if (!isCurrent()) return;
      const models = Array.isArray(res.models) ? res.models : [];
      if (!res.reachable) {
        modelDiscoveryStatus(res.error || 'Could not reach this endpoint. Check the URL and API key.');
      } else if (!models.length) {
        modelDiscoveryStatus(/11434|ollama/i.test(baseUrl)
          ? 'No models installed. Install one with ollama pull <model>, then discover again.'
          : 'No models returned by this endpoint. Check the provider or enter a custom model name.');
      } else {
        const selected = el('primaryModelInput').value.trim() || models[0];
        populateModelDropdowns(models, selected);
        const missing = !models.includes(selected) ? ' The current model was not returned; choose an installed model or keep it as a custom name.' : '';
        modelDiscoveryStatus(`${models.length} model${models.length === 1 ? '' : 's'} found.${missing}`, true);
      }
    } catch (err) {
      if (isCurrent() && err.name !== 'AbortError') modelDiscoveryStatus(err.message);
    } finally {
      if (state.modelProbeRequest === request) {
        state.modelProbeRequest = null;
        probeBtn.disabled = false;
        probeBtn.textContent = 'Discover models';
      }
    }
  }

  async function testModelConnection() {
    const baseUrl = el('modelBaseUrl').value.trim();
    const model = el('primaryModelInput').value.trim();
    const timeoutSeconds = Number(el('modelTimeoutInput').value);
    if (!baseUrl || !model) {
      showToast('Please specify both Base URL and Primary Model.', true);
      return;
    }
    if (!el('modelTimeoutInput').checkValidity()) {
      showToast('Set the inference timeout between 10 and 3600 seconds.', true);
      el('modelTimeoutInput').focus();
      return;
    }
    state.modelTestRequest?.abort();
    const request = new AbortController();
    state.modelTestRequest = request;
    const authEpoch = state.authEpoch;
    const apiKey = el('modelApiKey').value.trim();
    const isCurrent = () => state.modelTestRequest === request && authEpoch === state.authEpoch
      && getToken() && el('modelStudioDialog').open && el('modelBaseUrl').value.trim() === baseUrl
      && el('modelApiKey').value.trim() === apiKey && el('primaryModelInput').value.trim() === model
      && Number(el('modelTimeoutInput').value) === timeoutSeconds;
    const testBtn = el('testModelBtn');
    const originalText = testBtn.innerHTML;
    testBtn.disabled = true;
    testBtn.innerHTML = '<span>Testing...</span>';

    const testCard = el('modelTestCard');
    const statusBadge = el('testStatusBadge');
    const latencyEl = el('testLatency');
    const snippetEl = el('testOutputSnippet');

    testCard.classList.remove('hidden');
    statusBadge.className = 'badge';
    statusBadge.textContent = 'RUNNING TEST...';
    latencyEl.textContent = '...';
    snippetEl.textContent = `Sending a JSON completion probe. Waiting up to ${timeoutSeconds} seconds…`;

    try {
      const res = await api('/api/v1/models/test', {
        method: 'POST', signal: request.signal,
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model, timeout_seconds: timeoutSeconds }),
      });

      if (!isCurrent()) return;
      latencyEl.textContent = `${res.latency_ms}ms`;
      if (res.success) {
        statusBadge.className = 'badge badge-green';
        statusBadge.textContent = res.structured_output ? 'VERIFIED · JSON READY' : 'SUCCESS · RAW TEXT';
        snippetEl.textContent = `Response: ${res.response_snippet || 'OK'}`;
        showToast(`✓ ${model} verified successfully (${res.latency_ms}ms)`);
      } else {
        statusBadge.className = 'badge badge-red';
        statusBadge.textContent = 'FAILED';
        snippetEl.textContent = `Error: ${res.error || 'Connection failed'}`;
        showToast(`Model test failed: ${res.error}`, true);
      }
    } catch (err) {
      if (!isCurrent() || err.name === 'AbortError') return;
      statusBadge.className = 'badge badge-red';
      statusBadge.textContent = 'ERROR';
      snippetEl.textContent = err.message;
      showToast(err.message, true);
    } finally {
      if (state.modelTestRequest === request) {
        state.modelTestRequest = null;
        testBtn.disabled = false;
        testBtn.innerHTML = originalText;
      }
    }
  }

  function handleProviderChipClick(chip) {
    cancelModelRequests();
    modelSelectionChanged();
    document.querySelectorAll('.provider-chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');

    const url = chip.dataset.url;
    const model = chip.dataset.model;
    const fallbacks = chip.dataset.fallbacks;

    el('modelBaseUrl').value = url || '';
    el('modelApiKey').value = ''; // Never carry credentials to a different provider.
    if (model) {
      el('primaryModelInput').value = model;
      if (el('primaryModelSelect')) el('primaryModelSelect').value = model;
    }
    el('fallbackModelsInput').value = fallbacks || '';
    if (!model) el('primaryModelInput').value = '';
    populateModelDropdowns([], model || '');
    modelDiscoveryStatus('Discover models after entering this provider’s URL and API key.');
    syncProviderChips();

    if (chip.dataset.provider === 'ollama') {
      el('modelApiKey').value = '';
      el('apiKeyStatusHint').textContent = 'Ollama local models do not require an API key.';
      void probeModels();
    } else if (chip.dataset.provider === 'openai') {
      el('apiKeyStatusHint').textContent = 'Enter your OpenAI API key (sk-...).';
    } else if (chip.dataset.provider === 'openrouter') {
      el('apiKeyStatusHint').textContent = 'Enter your OpenRouter API key (sk-or-...).';
    } else if (chip.dataset.provider === 'deepseek') {
      el('apiKeyStatusHint').textContent = 'Enter your DeepSeek API key (sk-...).';
    } else if (chip.dataset.provider === 'groq') {
      el('apiKeyStatusHint').textContent = 'Enter your Groq API key (gsk_...).';
    }

  }

  // Model Studio Event Listeners
  el('modelStudioDialog').addEventListener('close', () => {
    // Native close events are queued; an older close must not cancel a reopened dialog.
    if (!el('modelStudioDialog').open) {
      state.modelFormRevision += 1;
      cancelModelRequests();
    }
  });
  for (const event of ['input', 'change']) {
    el('modelStudioForm').addEventListener(event, () => {
      state.modelFormDirty = true;
      state.modelFormRevision += 1;
    });
  }
  for (const id of ['modelBaseUrl', 'modelApiKey']) {
    el(id).addEventListener('input', () => {
      cancelModelRequests();
      populateModelDropdowns([], el('primaryModelInput').value.trim());
      syncProviderChips();
      modelDiscoveryStatus('Connection changed. Discover models to refresh the list.');
    });
    el(id).addEventListener('change', () => { void probeModels(); });
  }
  const modelStudioBtn = el('modelStudioBtn');
  if (modelStudioBtn) modelStudioBtn.addEventListener('click', openModelStudio);

  const quickConfigBtn = el('quickConfigModelBtn');
  if (quickConfigBtn) quickConfigBtn.addEventListener('click', openModelStudio);

  const closeModelStudioBtn = el('closeModelStudio');
  if (closeModelStudioBtn) closeModelStudioBtn.addEventListener('click', closeModelStudio);

  const modelStudioForm = el('modelStudioForm');
  if (modelStudioForm) modelStudioForm.addEventListener('submit', saveModelConfig);

  const probeModelsBtn = el('probeModelsBtn');
  if (probeModelsBtn) probeModelsBtn.addEventListener('click', () => probeModels());

  const testModelBtn = el('testModelBtn');
  if (testModelBtn) testModelBtn.addEventListener('click', testModelConnection);
  el('modelTimeoutInput').addEventListener('input', cancelModelTest);

  const primaryModelSelect = el('primaryModelSelect');
  if (primaryModelSelect) {
    primaryModelSelect.addEventListener('change', (e) => {
      modelSelectionChanged();
      const val = e.target.value;
      const input = el('primaryModelInput');
      if (val === '__custom__') {
        if (input) {
          input.value = '';
          input.focus();
        }
      } else if (val && input) {
        input.value = val;
      }
    });
  }

  const primaryModelInput = el('primaryModelInput');
  if (primaryModelInput) {
    primaryModelInput.addEventListener('input', (e) => {
      modelSelectionChanged();
      const val = e.target.value;
      if (primaryModelSelect) {
        const optionExists = Array.from(primaryModelSelect.options).some(opt => opt.value === val);
        if (optionExists) {
          primaryModelSelect.value = val;
        } else {
          primaryModelSelect.value = '__custom__';
        }
      }
    });
  }

  const toggleEye = el('toggleApiKeyVisibility');
  if (toggleEye) {
    toggleEye.addEventListener('click', () => {
      const input = el('modelApiKey');
      if (input.type === 'password') {
        input.type = 'text';
        toggleEye.textContent = '🔒';
      } else {
        input.type = 'password';
        toggleEye.textContent = '👁️';
      }
    });
  }

  document.querySelectorAll('.provider-chip').forEach(chip => {
    chip.addEventListener('click', () => handleProviderChipClick(chip));
  });

  // Event Search Input
  const eventSearchInput = el('eventSearchInput');
  if (eventSearchInput) {
    eventSearchInput.addEventListener('input', (e) => {
      state.searchQuery = e.target.value;
      renderEvents(state.events);
    });
  }

  // Modal Closers
  el('closeDrawer').addEventListener('click', () => closeDialog(el('taskDrawer')));
  el('closeArtifactModal').addEventListener('click', () => closeDialog(el('artifactDialog')));

  // Timeline Filter Pills
    el('timelineFilters').addEventListener('click', (e) => {
    if (e.target.tagName === 'BUTTON') {
      document.querySelectorAll('#timelineFilters .pill').forEach(p => { p.classList.remove('active'); p.setAttribute('aria-pressed', 'false'); });
      e.target.classList.add('active');
      e.target.setAttribute('aria-pressed', 'true');
      state.currentFilter = e.target.dataset.filter || 'ALL';
      renderEvents(state.events);
    }
  });

  window.addEventListener('resize', () => {
    if (state.view === 'run' && state.taskView === 'graph') drawDAGConnectors(state.tasks);
  });

  // Event Listeners & Binding
  el('openAuth').addEventListener('click', openAuth);

  el('clearToken').addEventListener('click', () => {
    state.authEpoch += 1;
    invalidateRepository();
    clearToken();
    el('adminToken').value = '';
    el('authBtnText').textContent = 'Connect workspace';
    stopRunUpdates(); resetRunsBrowser();
    state.tasks = []; state.events = []; state.currentRun = null; state.runId = '';
    closeModelStudio();
    state.modelConfig = null; populateModelDropdowns([], '');
    el('topbarModelName').textContent = 'Connect to configure';
    backToRuns();
    showToast('Session token cleared.');
    closeAuth();
  });

  el('authForm').addEventListener('submit', connectWorkspace);
  el('authDialog').addEventListener('close', () => { if (!el('authDialog').open) cancelAuthRequest(); });
  el('adminToken').addEventListener('input', () => { cancelAuthRequest(); feedback('authFeedback', ''); });

  el('browseFolderBtn').addEventListener('click', selectDirectory);
  el('dirPickerFallback').addEventListener('change', handleFallbackDirPicker);
  el('projectForm').addEventListener('submit', registerProject);
  el('runForm').addEventListener('submit', startRun);

  el('lookupForm').addEventListener('submit', (e) => {
    e.preventDefault();
    void loadRun(el('runLookup').value);
  });

  el('refreshRun').addEventListener('click', () => void loadRun(state.runId, { refresh: true }));

  el('copyRunId').addEventListener('click', async () => {
    if (!state.runId) return;
    await copyToClipboard(state.runId);
    showToast('Run ID copied to clipboard.');
  });

  // Switch to New Mission Launchpad
  const legacyNewRunBtn = el('newRunBtn');
  if (legacyNewRunBtn) {
    legacyNewRunBtn.addEventListener('click', () => {
      window.clearTimeout(state.pollTimer);
      if (state.ws) {
        state.ws.close();
        state.ws = null;
      }
      showLaunchpad();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  // Brand Logo Click: back to the Runs Browser.
  const brandLogo = el('brandLogo');
  if (brandLogo) {
    brandLogo.addEventListener('click', (e) => {
      e.preventDefault();
      backToRuns();
    });
  }

  // Prompt Preset Chips Click Binding
  document.querySelectorAll('.preset-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const goal = chip.dataset.goal;
      if (goal) {
        el('runGoal').value = goal;
        showToast(`Preset loaded: ${chip.dataset.title || 'Goal'}`);
        el('runGoal').focus();
      }
    });
  });

  // Clear Recent Runs Button
  const clearRecentBtn = el('clearRecentRuns');
  if (clearRecentBtn) {
    clearRecentBtn.addEventListener('click', () => {
      localStorage.removeItem(RECENT_RUNS_KEY);
      renderRecentRuns();
      showToast('Recent runs cleared.');
    });
  }

  // View router: Runs Browser <-> Mission Console <-> Launchpad.
  function showLaunchpad() { setView('new'); }
  function backToRuns() { setView('runs'); }

  function paletteActions() {
    const items = [
      { id: 'runs', title: 'Go to all runs', icon: '▦', hint: 'overview', keywords: 'runs list missions browse', run: backToRuns },
      { id: 'new', title: 'New run', icon: '＋', hint: 'launch', keywords: 'new run launch start goal create', run: showLaunchpad },
    ];
    if (state.view === 'run' && state.currentRun) {
      items.push({
        id: 'copyRun',
        title: 'Copy Run ID',
        icon: '⧉',
        hint: state.runId.slice(0, 8),
        keywords: 'copy id clipboard uuid',
        run: () => {
          void copyToClipboard(state.runId);
          showToast('Run ID copied to clipboard');
        },
      });
      if (!terminalStates.has(state.currentRun.state)) items.push({
        id: 'cancel',
        title: 'Cancel Current Run',
        icon: '✕',
        hint: 'destructive',
        keywords: 'cancel abort stop mission',
        run: openCancelDialog,
      });
      if (state.currentRun?.project_id && state.tasks.length) {
        items.push({
          id: 'intel',
          title: 'Jump to Latest Task Reasoning',
          icon: '⤷',
          hint: 'drawer',
          keywords: 'agent reasoning messages summaries drawer intel',
          run: () => {
            const running = state.tasks.find(t => t.state === 'RUNNING' || t.state === 'LEASED') || state.tasks[0];
            if (running) openTaskDrawer(running);
          },
        });
      }
    }
    for (const recent of getRecentRuns()) {
      items.push({
        id: `run:${recent.id}`,
        title: `Open ${recent.goal || recent.id}`.slice(0, 80),
        icon: '▸',
        hint: recent.id.slice(0, 8),
        keywords: `open run mission ${recent.id}`,
        run: () => { void loadRun(recent.id); },
      });
    }
    items.push({
      id: 'studio',
      title: 'Open Model Studio',
      icon: '⚙',
      hint: 'providers',
      keywords: 'model studio llm provider config api key endpoint',
      run: openModelStudio,
    });
    return items;
  }

  // Topbar navigation tabs.
  document.querySelectorAll('[data-nav]').forEach((tab) => {
    tab.addEventListener('click', (event) => {
      event.preventDefault();
      const target = tab.dataset.nav;
      if (target === 'runs') backToRuns();
      else if (target === 'new') showLaunchpad();
    });
  });

  // One handler per action; native dialogs retain focus and Escape behavior.
  document.querySelector('.skip-link').addEventListener('click', event => {
    event.preventDefault();
    el('mainContent').focus({ preventScroll: true });
    el('mainContent').scrollIntoView({ block: 'start' });
  });
  el('openCommandPalette').addEventListener('click', openPalette);
  el('backToRuns').addEventListener('click', backToRuns);
  el('closeAuth').addEventListener('click', closeAuth);
  el('approvalForm').addEventListener('submit', submitApproval);
  el('cancelRun').addEventListener('click', openCancelDialog);
  el('cancelRunForm').addEventListener('submit', submitCancellation);
  for (const id of ['closeCancelRun', 'keepRun']) el(id).addEventListener('click', () => closeDialog(el('cancelRunDialog')));
  el('cancelRunDialog').addEventListener('close', () => { if (!el('cancelRunDialog').open) state.cancelTarget = null; });
  el('closeApproval').addEventListener('click', () => closeDialog(el('approvalDialog')));
  el('approvalDialog').addEventListener('close', () => { if (!el('approvalDialog').open) state.approvalDecision = null; });
  el('artifactDialog').addEventListener('close', () => {
    if (!el('artifactDialog').open) { state.artifactRequest?.abort(); state.artifactRequest = null; }
  });
  el('taskDrawer').addEventListener('close', () => { if (!el('taskDrawer').open) clearTaskIntel(el('drawerTaskIntel')); });
  for (const id of ['projectName', 'sourcePath', 'defaultBranch']) el(id).addEventListener('input', invalidateRepository);
  el('baselineCommit').addEventListener('invalid', () => { el('baselineCommit').closest('details').open = true; });
  document.querySelectorAll('[data-panel]').forEach(tab => {
    tab.addEventListener('click', () => selectPanel(tab.dataset.panel));
    tab.addEventListener('keydown', event => {
      const tabs = [...document.querySelectorAll('[data-panel]')];
      let index = tabs.indexOf(tab);
      if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else return;
      event.preventDefault(); selectPanel(tabs[index].dataset.panel); tabs[index].focus();
    });
  });
  for (const [id, view] of [['taskListView', 'list'], ['taskGraphView', 'graph']]) el(id).addEventListener('click', () => {
    state.taskView = view;
    el('taskList').classList.toggle('hidden', view !== 'list');
    el('dagViewport').classList.toggle('hidden', view !== 'graph');
    el('taskListView').setAttribute('aria-pressed', String(view === 'list'));
    el('taskGraphView').setAttribute('aria-pressed', String(view === 'graph'));
    if (view === 'graph') renderDAG(state.tasks);
  });
  function restoreRoute() {
    const hash = window.location.hash;
    if (hash.startsWith('#run/')) {
      let id;
      try { id = decodeURIComponent(hash.slice(5)); } catch (_) { setView('runs', '', 'replace'); return; }
      void loadRun(id, { historyMode: 'none' });
    } else setView(hash === '#new' ? 'new' : 'runs', '', 'none');
  }
  window.addEventListener('popstate', restoreRoute);
  window.addEventListener('hashchange', () => {
    const expected = state.view === 'run' ? `#run/${encodeURIComponent(state.runId)}` : `#${state.view}`;
    if (window.location.hash !== expected) restoreRoute();
  });
  el('runsNewBtn').addEventListener('click', showLaunchpad);
  initPalette(paletteActions);
  setUnauthorizedHandler(() => {
    el('authBtnText').textContent = 'Connect workspace';
  });
  initRunsBrowser({ onOpenRun: runId => void loadRun(runId), onNewRun: showLaunchpad, onConnect: openAuth });
  initTour();
  void checkHealth();
  window.setInterval(checkHealth, 30000);
  restoreIdentity();
  if (getToken()) void loadModelConfig();
  restoreRoute();
})();
