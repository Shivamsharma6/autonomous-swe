import { api, setUnauthorizedHandler, getToken, setToken, clearToken, connectTaskSocket } from './js/api.js';
import {
  el,
  terminalStates,
  escapeHtml,
  formatDuration,
  formatBytes,
  summarizeCounts,
  stateTone,
  copyToClipboard,
} from './js/util.js';
import { showToast } from './js/toast.js';
import { initRunsBrowser, showRunsBrowser, hideRunsBrowser } from './js/runsBrowser.js';
import { initPalette, openPalette } from './js/palette.js';
import { loadTaskIntel, clearTaskIntel } from './js/taskIntel.js';
import { renderSparkline, fetchHistory, pushLiveSample, getLiveHistory } from './js/sparkline.js';
import { createVirtualList } from './js/virtualList.js';
import { initTour } from './js/tour.js';

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
    historyCache: new Map(),
  };

  // Virtualized timeline — initialized lazily on first render
  let timelineVirtual = null;
  function ensureTimelineVirtual() {
    if (timelineVirtual) return timelineVirtual;
    const container = el('terminalScrollContainer');
    if (!container) return null;
    timelineVirtual = createVirtualList({
      container,
      rowHeight: 56,
      overscan: 10,
      renderRow: (event) => {
        const item = document.createElement('li');
        item.className = 'timeline-item';
        const time = document.createElement('span');
        time.className = 'timeline-time';
        time.textContent = new Date(event.created_at).toLocaleTimeString();
        const name = document.createElement('span');
        name.className = 'timeline-event-name';
        name.textContent = event.event_type;
        const data = document.createElement('span');
        data.className = 'timeline-data';
        try {
          data.textContent = JSON.stringify(event.payload).slice(0, 400);
        } catch (_) { data.textContent = String(event.payload); }
        item.append(time, name, data);
        return item;
      },
    });
    return timelineVirtual;
  }

  // Authentication Dialog Controls
  function openAuth() {
    el('adminToken').value = getToken();
    if (!el('authDialog').open) el('authDialog').showModal();
  }

  function closeAuth() {
    if (el('authDialog').open) el('authDialog').close();
  }

  // Recent Runs Manager (Local Storage)
  const RECENT_RUNS_KEY = 'autoswe.recentRuns';

  function getRecentRuns() {
    try {
      return JSON.parse(localStorage.getItem(RECENT_RUNS_KEY)) || [];
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
      chip.innerHTML = `<span>#${run.id.slice(0, 8)}</span> <span style="opacity: 0.7;">· ${run.goal}</span>`;
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
        el('healthText').textContent = 'Platform Ready';
      } else if (coreReady && unavailable.length === 1 && unavailable[0] === 'uams') {
        el('healthDot').className = 'status-dot ready';
        el('healthText').textContent = 'Platform Ready (UAMS offline)';
      } else {
        el('healthDot').className = 'status-dot failed';
        el('healthText').textContent = `Degraded: ${unavailable.join(', ') || 'dependencies'}`;
      }
    } catch (_) {
      el('healthDot').className = 'status-dot failed';
      el('healthText').textContent = 'Control Plane Offline';
    }
  }

  // Restore Session Storage State
  function restoreIdentity() {
    if (getToken()) {
      el('authBtnText').textContent = 'Admin Connected';
    }
    if (state.projectId && state.repositoryId) {
      const ident = el('projectIdentity');
      if (ident) {
        const textSpan = ident.querySelector('.identity-text');
        if (textSpan) textSpan.textContent = `${state.projectName || 'Project'} · ${state.projectId}`;
      }
      el('startRun').disabled = false;
    }
    renderRecentRuns();
    if (state.runId) {
      el('runLookup').value = state.runId;
      void loadRun(state.runId);
    }
    if (!getToken()) {
      window.setTimeout(openAuth, 300);
    }
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

  async function onboardRepository(payload) {
    const body = await api('/api/v1/projects/onboard', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.projectId = body.project_id;
    state.repositoryId = body.repository_id;
    state.projectName = body.name;
    sessionStorage.setItem('autoswe.projectId', state.projectId);
    sessionStorage.setItem('autoswe.repositoryId', state.repositoryId);
    sessionStorage.setItem('autoswe.projectName', state.projectName);

    el('projectName').value = body.name;
    el('sourcePath').value = body.source_path;
    el('defaultBranch').value = body.default_branch;
    if (body.baseline_commit) {
      el('baselineCommit').value = body.baseline_commit;
    }

    const ident = el('projectIdentity');
    if (ident) {
      const textSpan = ident.querySelector('.identity-text');
      if (textSpan) {
        textSpan.innerHTML = `<strong>✓ Ready</strong> · ${body.name} (${body.default_branch} · <code>${body.baseline_commit ? body.baseline_commit.slice(0, 8) : 'HEAD'}</code>)`;
      }
    }
    el('startRun').disabled = false;
    return body;
  }

  // Directory Picker Fallback & Local Git Inspection
  async function selectDirectory(event) {
    if (event) event.preventDefault();
    if (window.showDirectoryPicker) {
      try {
        const dirHandle = await window.showDirectoryPicker({ mode: 'read' });
        if (!dirHandle) return;
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
        });

        showToast(`✓ "${dirName}" ready (${onboardRes.default_branch} · ${onboardRes.baseline_commit.slice(0, 8)})`);
        return;
      } catch (err) {
        if (err.name === 'AbortError') return;
        console.warn('showDirectoryPicker error, falling back to input:', err);
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
        });
        showToast(`✓ "${dirName}" ready (${onboardRes.default_branch} · ${onboardRes.baseline_commit.slice(0, 8)})`);
      } catch (err) {
        showToast(err.message, true);
      }
    }
  }

  // Register New Project Repository
  async function registerProject(event) {
    event.preventDefault();
    try {
      showToast('Registering repository...');
      const body = await onboardRepository({
        name: el('projectName').value.trim(),
        source_path: el('sourcePath').value.trim(),
        folder_name: el('sourcePath').value.trim(),
        default_branch: el('defaultBranch').value.trim(),
      });
      showToast(`Repository "${body.name}" registered and ready.`);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  // Start Autonomous Mission Run
  async function startRun(event) {
    event.preventDefault();
    const runBtn = el('startRun');
    const originalText = runBtn.innerHTML;
    runBtn.disabled = true;

    try {
      if (!state.projectId || !state.repositoryId) {
        showToast('Auto-provisioning repository...');
        const onboardRes = await onboardRepository({
          name: el('projectName').value.trim(),
          source_path: el('sourcePath').value.trim(),
          folder_name: el('sourcePath').value.trim(),
          default_branch: el('defaultBranch').value.trim(),
        });
        if (!el('baselineCommit').value.trim()) {
          el('baselineCommit').value = onboardRes.baseline_commit;
        }
      }

      const commitSha = el('baselineCommit').value.trim().toLowerCase();
      if (!commitSha || commitSha.length < 40) {
        showToast('Baseline commit SHA must be a 40–64 char hex string.', true);
        runBtn.disabled = false;
        return;
      }

      showToast('Launching agentic mission...');
      const goalText = el('runGoal').value.trim();
      const body = await api('/api/v1/runs', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          repository_id: state.repositoryId,
          goal: goalText,
          baseline_commit: commitSha,
        }),
      });
      state.runId = body.run_id;
      sessionStorage.setItem('autoswe.runId', state.runId);
      el('runLookup').value = state.runId;
      saveRecentRun(state.runId, goalText);
      showToast('Run launched. Architect agent is synthesizing the DAG.');
      await loadRun(state.runId);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      runBtn.disabled = false;
      runBtn.innerHTML = originalText;
    }
  }

  // Main Load Run Controller
  async function loadRun(runId) {
    const candidate = String(runId || '').trim();
    if (!candidate) return;
    state.runId = candidate;
    sessionStorage.setItem('autoswe.runId', candidate);
    window.clearTimeout(state.pollTimer);

    try {
      const [run, tasks, approvals, artifacts, events] = await Promise.all([
        api(`/api/v1/runs/${encodeURIComponent(candidate)}`),
        api(`/api/v1/runs/${encodeURIComponent(candidate)}/tasks`),
        api(`/api/v1/runs/${encodeURIComponent(candidate)}/approvals`),
        api(`/api/v1/runs/${encodeURIComponent(candidate)}/artifacts`),
        api(`/api/v1/runs/${encodeURIComponent(candidate)}/events?limit=500`),
      ]);

      state.tasks = tasks || [];
      state.approvals = approvals || [];
      state.artifacts = artifacts || [];
      state.events = events || [];

      saveRecentRun(candidate, run.goal);
      hideRunsBrowser();
      el('launchpadSection')?.classList.add('hidden');
      document.querySelectorAll('[data-nav]').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.nav === 'mission');
      });
      el('dashboard').classList.remove('hidden');
      renderRun(run);
      setupWebSocket(run.project_id, candidate);

      if (!terminalStates.has(run.state)) {
        state.pollTimer = window.setTimeout(() => void loadRun(candidate), 3000);
      }
    } catch (error) {
      el('streamStatusText').textContent = 'POLLING PAUSED';
      el('liveStreamBadge').classList.remove('live');
      showToast(error.message, true);
    }
  }

  // Live event streaming over the hardened subprotocol-authenticated socket,
  // with exponential-backoff reconnect while the task stays active.
  let wsRetryDelay = 1000;

  function setupWebSocket(projectId, runId) {
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    window.clearTimeout(state.wsReconnectTimer);

    const runningTask = state.tasks.find(t => t.state === 'RUNNING' || t.state === 'LEASED');
    if (!runningTask) {
      el('streamStatusText').textContent = 'POLLING ACTIVE';
      return;
    }

    const handleEvent = (payload) => {
      state.events.unshift(payload);
      renderEvents(state.events);
    };
    const handleState = (status) => {
      if (status === 'open') {
        wsRetryDelay = 1000;
        el('liveStreamBadge').classList.add('live');
        el('streamStatusText').textContent = 'LIVE WEBSOCKET';
      } else if (status === 'error') {
        el('liveStreamBadge').classList.remove('live');
        el('streamStatusText').textContent = 'POLLING FALLBACK';
      } else if (status === 'close') {
        state.ws = null;
        if (!state.runId) return;
        const stillActive = state.tasks.some(t => t.state === 'RUNNING' || t.state === 'LEASED');
        if (stillActive && wsRetryDelay <= 8000) {
          window.setTimeout(() => {
            if (state.runId === runId) setupWebSocket(projectId, runId);
          }, wsRetryDelay);
          wsRetryDelay *= 2;
        }
      }
    };

    state.ws = connectTaskSocket(projectId, runningTask.id, { onEvent: handleEvent, onState: handleState });
  }

  // Render Dashboard
  function renderRun(run) {
    el('onboardingSection').classList.add('hidden');
    el('dashboard').classList.remove('hidden');
    el('runGoalTitle').textContent = run.goal;
    el('runIdText').textContent = run.run_id;
    el('runProjectName').textContent = state.projectName || run.project_id.slice(0, 8);
    
    // Status Badge
    const statusBadge = el('runStatusBadge');
    statusBadge.textContent = run.state;
    statusBadge.className = `status-badge ${run.state.toLowerCase()}`;

    // Metrics
    el('runState').textContent = run.state;
    el('stateDuration').textContent = `${formatDuration(run.state_duration_seconds)} in current state`;
    el('planRevision').textContent = run.active_plan_revision === null ? 'Planning' : `r${run.active_plan_revision}`;
    el('taskSummary').textContent = summarizeCounts(run.task_counts);

    // Calculate Task Progress Fill
    const taskCounts = run.task_counts || {};
    const totalTasks = Object.values(taskCounts).reduce((a, b) => a + b, 0);
    const completedTasks = taskCounts.COMPLETED || 0;
    const pct = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
    const progressFill = el('dagProgressBar');
    if (progressFill) progressFill.style.width = `${pct}%`;

    const totalTokens = (run.model_input_tokens || 0) + (run.model_output_tokens || 0);
    {
      const tokenEl = el('tokenTotal');
      const newVal = totalTokens.toLocaleString();
      if (tokenEl.textContent !== newVal) {
        tokenEl.textContent = newVal;
        tokenEl.classList.remove('pulse'); void tokenEl.offsetWidth; tokenEl.classList.add('pulse');
        window.setTimeout(() => tokenEl.classList.remove('pulse'), 600);
      }
    }
    el('tokenDetail').textContent = `${(run.model_input_tokens || 0).toLocaleString()} in · ${(run.model_output_tokens || 0).toLocaleString()} out`;
    {
      const costEl = el('modelCost');
      const newVal = `$${Number(run.model_cost_usd || 0).toFixed(4)}`;
      if (costEl.textContent !== newVal) {
        costEl.textContent = newVal;
        costEl.classList.remove('pulse'); void costEl.offsetWidth; costEl.classList.add('pulse');
        window.setTimeout(() => costEl.classList.remove('pulse'), 600);
      }
    }

    // --- Live sparkline history (HUD) ---
    pushLiveSample(run.run_id, {
      input_tokens: run.model_input_tokens,
      output_tokens: run.model_output_tokens,
      cost_usd: run.model_cost_usd,
      model: '',
    });
    const hudCostEl = el('costSparkline');
    const hudTokenEl = el('tokenSparkline');
    if (hudCostEl || hudTokenEl) {
      const cached = state.historyCache.get(run.run_id);
      const doRender = (samples) => {
        const costSamples = samples.map((s) => ({ cost_usd: s.cost_usd }));
        const tokenSamples = samples.map((s) => ({ cost_usd: (s.input_tokens || 0) + (s.output_tokens || 0) }));
        if (hudCostEl) renderSparkline(hudCostEl, costSamples, { width: 100, height: 22, stroke: 'var(--accent-emerald)', fill: 'rgba(16,185,129,0.08)' });
        if (hudTokenEl) renderSparkline(hudTokenEl, tokenSamples, { width: 100, height: 22, stroke: 'var(--accent-cyan)', fill: 'rgba(6,182,212,0.08)' });
      };
      if (cached) {
        doRender(getLiveHistory(run.run_id, cached));
      } else {
        fetchHistory(run.run_id).then((samples) => {
          state.historyCache.set(run.run_id, samples);
          doRender(getLiveHistory(run.run_id, samples));
        });
      }
    }

    renderDAG(state.tasks);
    renderApprovals(state.approvals);
    renderArtifacts(state.artifacts, run.project_id);
    renderEvents(state.events);
  }

  // Topological DAG Layout & Stage Grouping Engine
  function isRunningState(state) { return state === 'RUNNING' || state === 'LEASED'; }

  function renderDAG(tasks) {
    const root = el('taskDag');
    const svg = el('dagSvgConnections');
    root.replaceChildren();
    svg.replaceChildren();

    if (!tasks || !tasks.length) {
      root.innerHTML = '<div class="empty-state"><div class="empty-spinner"></div><p>Architect agent is synthesizing the execution DAG...</p></div>';
      return;
    }

    // Compute topological ranks for each task
    const taskMap = new Map(tasks.map(t => [t.id, t]));
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

    tasks.forEach(t => getRank(t.id));

    // Group tasks by rank
    const maxRank = Math.max(...Array.from(ranks.values()), 0);
    const columns = Array.from({ length: maxRank + 1 }, () => []);

    tasks.forEach(t => {
      const rank = ranks.get(t.id) || 0;
      columns[rank].push(t);
    });

    const levelNames = [
      'Stage 1: Discovery & Research',
      'Stage 2: Architecture & Plan',
      'Stage 3: Implementation',
      'Stage 4: Verification & Test',
      'Stage 5: Review & Finalization'
    ];

    // Render Columns & Task Cards
    columns.forEach((columnTasks, levelIdx) => {
      const colEl = document.createElement('div');
      colEl.className = 'dag-column';

      const colHeader = document.createElement('div');
      colHeader.className = 'dag-column-header';
      
      const titleSpan = document.createElement('span');
      titleSpan.textContent = levelNames[levelIdx] || `Stage ${levelIdx + 1}`;
      
      const countBadge = document.createElement('span');
      countBadge.className = 'brand-version-pill';
      const completedCount = columnTasks.filter(t => t.state === 'COMPLETED').length;
      countBadge.textContent = `${completedCount}/${columnTasks.length}`;

      colHeader.append(titleSpan, countBadge);
      colEl.append(colHeader);

      columnTasks.forEach(task => {
        const node = document.createElement('article');
        node.className = `task-node ${task.state.toLowerCase()}${isRunningState(task.state) ? ' is-running' : ''}`;
        node.id = `node-${task.id}`;
        node.dataset.taskId = task.id;

        const header = document.createElement('div');
        header.className = 'task-node-header';
        
        const typeTag = document.createElement('span');
        typeTag.className = 'task-type-tag';
        typeTag.textContent = task.task_type;

        const statusPill = document.createElement('span');
        statusPill.className = `status-badge ${task.state.toLowerCase()}`;
        statusPill.textContent = task.state;
        header.append(typeTag, statusPill);

        const title = document.createElement('h4');
        title.className = 'task-node-title';
        title.textContent = task.title;

        const meta = document.createElement('div');
        meta.className = 'task-node-meta';
        const depsCount = task.dependencies ? task.dependencies.length : 0;
        meta.innerHTML = `<span>${task.assigned_capability}</span><span>${depsCount ? `${depsCount} dep${depsCount > 1 ? 's' : ''}` : 'Root'}</span>`;

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
      const childNode = el(`node-${task.id}`);
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
        path.setAttribute('stroke', isRunning ? 'url(#activeGrad)' : 'rgba(255, 255, 255, 0.15)');
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
    el('drawerTaskId').textContent = task.id;
    
    const stateEl = el('drawerTaskState');
    stateEl.textContent = task.state;
    stateEl.className = `state-pill ${task.state.toLowerCase()}`;

    el('drawerTaskCapability').textContent = task.assigned_capability;
    el('drawerTaskPriority').textContent = `Priority ${task.priority}`;
    el('drawerTaskDeps').textContent = task.dependencies && task.dependencies.length ? task.dependencies.join(', ') : 'None (Root)';
    el('drawerTaskRevision').textContent = `r${task.plan_revision}`;

    el('drawerTaskGoal').textContent = task.goal || task.title;

    // Agent reasoning feed (handoff summaries) for this task.
    void loadTaskIntel(state.projectId, task.id, el('drawerTaskIntel'));

    // Filter events for this task
    const taskEvents = state.events.filter(e => e.payload && e.payload.task_id === task.id);
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
          <span class="timeline-event-name">${evt.event_type}</span>
          <span class="timeline-data">${JSON.stringify(evt.payload)}</span>
        `;
        eventsContainer.append(item);
      });
    }

    el('taskDrawer').showModal();
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
  async function decideApproval(approval, approved) {
    const approver = window.prompt('Enter operator identity for the immutable audit log:');
    if (!approver || !approver.trim()) return;

    try {
      await api(`/api/v1/approvals/${encodeURIComponent(approval.approval_id)}/decision`, {
        method: 'POST',
        body: JSON.stringify({
          approved,
          approver: approver.trim(),
          expected_call_hash: approval.call_hash,
        }),
      });
      showToast(approved ? 'Tool call approved.' : 'Tool call rejected.');
      await loadRun(state.runId);
    } catch (error) {
      showToast(error.message, true);
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
      item.onclick = () => previewArtifact(projectId, art);
      root.append(item);
    });
  }

  // Artifact Preview & Diff Formatting
  async function previewArtifact(projectId, artifact) {
    el('modalArtifactType').textContent = artifact.media_type;
    el('modalArtifactTitle').textContent = `Artifact ${artifact.artifact_id.slice(0, 8)}`;
    el('modalArtifactMeta').textContent = `SHA-256: ${artifact.sha256} • Size: ${formatBytes(artifact.size_bytes)}`;
    el('artifactPreviewCode').textContent = 'Fetching and verifying object content...';
    el('downloadArtifactBtn').onclick = () => downloadArtifact(projectId, artifact);
    el('artifactDialog').showModal();

    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!response.ok) throw new Error(`Fetch failed (${response.status})`);
      const text = await response.text();
      
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

    // Virtualized rendering for large audit trails (500+ events)
    const vt = ensureTimelineVirtual();
    if (vt && filtered.length > 40) {
      vt.setItems(filtered);
      // Hover pauses auto-scroll: track via data attribute
      const container = el('terminalScrollContainer');
      if (container && !container._virtualHoverBound) {
        container._virtualHoverBound = true;
        let paused = false;
        container.addEventListener('mouseenter', () => { paused = true; container.dataset.paused = '1'; });
        container.addEventListener('mouseleave', () => { paused = false; container.dataset.paused = '0'; });
      }
      return;
    }

    // Fallback: direct render for small lists (<40) or if virtual not ready
    const root = el('eventList');
    if (!root) return;
    // Ensure virtual viewport hidden when falling back
    if (vt) {
      const vp = document.querySelector('.virtual-viewport');
      if (vp) vp.style.display = 'none';
      root.style.display = '';
    }
    root.replaceChildren();
    if (!filtered.length) {
      root.innerHTML = '<li class="timeline-empty">No matching events found in audit trail.</li>';
      return;
    }
    // Show virtual content if previously hidden
    if (vt) {
      const vp = document.querySelector('.virtual-viewport');
      if (vp) vp.style.display = '';
    }
    const frag = document.createDocumentFragment();
    for (const event of filtered) {
      const item = document.createElement('li');
      item.className = 'timeline-item';
      const time = document.createElement('span');
      time.className = 'timeline-time';
      time.textContent = new Date(event.created_at).toLocaleTimeString();
      const name = document.createElement('span');
      name.className = 'timeline-event-name';
      name.textContent = event.event_type;
      const data = document.createElement('span');
      data.className = 'timeline-data';
      const raw = JSON.stringify(event.payload);
      data.textContent = raw.length > 400 ? `${raw.slice(0, 400)}…` : raw;
      data.title = raw.length > 400 ? 'Click to copy full payload' : '';
      if (raw.length > 400) {
        data.style.cursor = 'pointer';
        data.addEventListener('click', () => {
          void copyToClipboard(raw);
          showToast('Payload copied');
        });
      }
      item.append(time, name, data);
      frag.appendChild(item);
    }
    root.appendChild(frag);
  }

  // Utility Formatters
  function openModelStudio() {
    if (state.modelConfig) {
      if (el('modelBaseUrl')) el('modelBaseUrl').value = state.modelConfig.base_url || '';
      if (el('primaryModelInput')) el('primaryModelInput').value = state.modelConfig.primary_model || '';
      if (el('fallbackModelsInput')) el('fallbackModelsInput').value = (state.modelConfig.fallback_models || []).join(', ');
      if (el('modelTimeoutInput')) el('modelTimeoutInput').value = state.modelConfig.timeout_seconds || 300;
      if (el('modelTemperatureInput')) el('modelTemperatureInput').value = state.modelConfig.temperature || 0.0;
      if (state.modelConfig.has_api_key && el('apiKeyStatusHint')) {
        el('apiKeyStatusHint').textContent = `Active Key: ${state.modelConfig.api_key_preview || 'Configured (Masked)'}`;
      }
      populateModelDropdowns(state.modelConfig.fallback_models || [], state.modelConfig.primary_model);
    }
    if (el('modelTestCard')) el('modelTestCard').classList.add('hidden');
    if (!el('modelStudioDialog').open) el('modelStudioDialog').showModal();
    void probeModels(false);
  }

  function closeModelStudio() {
    if (el('modelStudioDialog').open) el('modelStudioDialog').close();
  }

  function updateModelBadge(config) {
    if (!config) return;
    const model = config.primary_model || 'Unknown';
    const provider = config.provider_name || 'LLM';
    
    let icon = '⚡';
    if (provider.includes('OpenAI')) icon = '✨';
    else if (provider.includes('OpenRouter') || provider.includes('Anthropic')) icon = '🧠';
    else if (provider.includes('Ollama')) icon = '🦙';
    else if (provider.includes('DeepSeek')) icon = '🐋';

    const topbarText = el('topbarModelName');
    if (topbarText) {
      topbarText.textContent = `${icon} ${model}`;
    }

    const providerLabel = el('launchpadProviderLabel');
    if (providerLabel) {
      providerLabel.textContent = `${provider} (${config.base_url})`;
    }
  }

  function populateModelDropdowns(models, selected) {
    const cleanModels = Array.isArray(models) ? models : [];
    const all = Array.from(new Set([selected, ...cleanModels, ...DEFAULT_MODELS].filter(Boolean)));

    // 1. Launchpad select
    const launchpadSelect = el('launchpadModelSelect');
    if (launchpadSelect) {
      launchpadSelect.innerHTML = '';
      if (cleanModels.length) {
        const groupDiscovered = document.createElement('optgroup');
        groupDiscovered.label = 'Discovered / Available Models';
        cleanModels.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = `⚡ ${m}`;
          if (m === selected) opt.selected = true;
          groupDiscovered.appendChild(opt);
        });
        launchpadSelect.appendChild(groupDiscovered);
      }
      const groupStandard = document.createElement('optgroup');
      groupStandard.label = 'Standard & Frontier Models';
      DEFAULT_MODELS.forEach(m => {
        if (!cleanModels.includes(m)) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === selected) opt.selected = true;
          groupStandard.appendChild(opt);
        }
      });
      launchpadSelect.appendChild(groupStandard);
      if (selected) launchpadSelect.value = selected;
    }

    // 2. Modal Primary Model select
    const primarySelect = el('primaryModelSelect');
    if (primarySelect) {
      primarySelect.innerHTML = '';
      all.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        if (m === selected) opt.selected = true;
        primarySelect.appendChild(opt);
      });
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = '✏️ Custom Model Name...';
      primarySelect.appendChild(customOpt);
      if (selected) primarySelect.value = selected;
    }

    // 3. Modal Primary Model input text box
    const primaryInput = el('primaryModelInput');
    if (primaryInput && selected) {
      primaryInput.value = selected;
    }

    // 4. Discovered chips container
    const chipsContainer = el('discoveredModelsChips');
    if (chipsContainer) {
      chipsContainer.innerHTML = '';
      if (cleanModels.length) {
        cleanModels.forEach(m => {
          const chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'preset-chip mini-chip';
          chip.style.cssText = 'padding: 2px 8px; font-size: 0.72rem; cursor: pointer; border-radius: 12px;';
          chip.textContent = m;
          chip.addEventListener('click', () => {
            if (primaryInput) primaryInput.value = m;
            if (primarySelect) primarySelect.value = m;
            if (launchpadSelect) launchpadSelect.value = m;
            showToast(`Selected model: ${m}`);
          });
          chipsContainer.appendChild(chip);
        });
      } else {
        chipsContainer.innerHTML = '<span style="font-size: 0.72rem; color: var(--text-muted);">Click "Discover Models" above to list models</span>';
      }
    }
  }

  async function loadModelConfig() {
    try {
      const config = await api('/api/v1/models/config');
      state.modelConfig = config;
      updateModelBadge(config);
      populateModelDropdowns(config.fallback_models || [], config.primary_model);
      void probeModels(false);
    } catch (_) {
      const topbarText = el('topbarModelName');
      if (topbarText) topbarText.textContent = '⚡ Configure Models';
      populateModelDropdowns([], 'gemma4:12b-mlx');
    }
  }

  async function saveModelConfig(event) {
    event.preventDefault();
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
      state.modelConfig = updated;
      updateModelBadge(updated);
      populateModelDropdowns(updated.fallback_models || [], updated.primary_model);
      closeModelStudio();
      showToast(`✓ Active Model: ${updated.primary_model} (${updated.provider_name})`);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function probeModels(notify = true) {
    const baseUrl = el('modelBaseUrl') ? el('modelBaseUrl').value.trim() : (state.modelConfig ? state.modelConfig.base_url : '');
    if (!baseUrl) {
      if (notify) showToast('Please enter an endpoint Base URL to probe.', true);
      return;
    }
    const apiKey = el('modelApiKey') ? el('modelApiKey').value.trim() : '';
    const probeBtn = el('probeModelsBtn');
    let originalText = '';
    if (probeBtn) {
      originalText = probeBtn.innerHTML;
      probeBtn.disabled = true;
      probeBtn.innerHTML = '<span>Probing...</span>';
    }

    try {
      if (notify) showToast(`Probing models at ${baseUrl}...`);
      const res = await api('/api/v1/models/probe', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
      });
      if (res.reachable && res.models && res.models.length) {
        const cur = el('primaryModelInput') ? el('primaryModelInput').value.trim() : (state.modelConfig ? state.modelConfig.primary_model : '');
        populateModelDropdowns(res.models, cur || res.models[0]);
        if (el('primaryModelInput') && !el('primaryModelInput').value.trim()) {
          el('primaryModelInput').value = res.models[0];
        }
        if (notify) showToast(`✓ Discovered ${res.models.length} models (${res.latency_ms}ms)`);
      } else {
        if (notify) showToast(`Probe result: ${res.error || 'No models returned, check URL and key.'}`, true);
      }
    } catch (err) {
      if (notify) showToast(err.message, true);
    } finally {
      if (probeBtn) {
        probeBtn.disabled = false;
        probeBtn.innerHTML = originalText;
      }
    }
  }

  async function testModelConnection() {
    const baseUrl = el('modelBaseUrl').value.trim();
    const model = el('primaryModelInput').value.trim();
    if (!baseUrl || !model) {
      showToast('Please specify both Base URL and Primary Model.', true);
      return;
    }
    const apiKey = el('modelApiKey').value.trim();
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
    snippetEl.textContent = 'Sending test JSON completion probe...';

    try {
      const res = await api('/api/v1/models/test', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model: model }),
      });

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
      statusBadge.className = 'badge badge-red';
      statusBadge.textContent = 'ERROR';
      snippetEl.textContent = err.message;
      showToast(err.message, true);
    } finally {
      testBtn.disabled = false;
      testBtn.innerHTML = originalText;
    }
  }

  function handleProviderChipClick(chip) {
    document.querySelectorAll('.provider-chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');

    const url = chip.dataset.url;
    const model = chip.dataset.model;
    const fallbacks = chip.dataset.fallbacks;

    if (url) el('modelBaseUrl').value = url;
    if (model) {
      el('primaryModelInput').value = model;
      if (el('primaryModelSelect')) el('primaryModelSelect').value = model;
    }
    if (fallbacks) el('fallbackModelsInput').value = fallbacks;

    if (chip.dataset.provider === 'ollama') {
      el('modelApiKey').value = '';
      el('apiKeyStatusHint').textContent = 'Ollama local models do not require an API key.';
    } else if (chip.dataset.provider === 'openai') {
      el('apiKeyStatusHint').textContent = 'Enter your OpenAI API key (sk-...).';
    } else if (chip.dataset.provider === 'openrouter') {
      el('apiKeyStatusHint').textContent = 'Enter your OpenRouter API key (sk-or-...).';
    } else if (chip.dataset.provider === 'deepseek') {
      el('apiKeyStatusHint').textContent = 'Enter your DeepSeek API key (sk-...).';
    } else if (chip.dataset.provider === 'groq') {
      el('apiKeyStatusHint').textContent = 'Enter your Groq API key (gsk_...).';
    }

    void probeModels(false);
  }

  // Model Studio Event Listeners
  const modelStudioBtn = el('modelStudioBtn');
  if (modelStudioBtn) modelStudioBtn.addEventListener('click', openModelStudio);

  const quickConfigBtn = el('quickConfigModelBtn');
  if (quickConfigBtn) quickConfigBtn.addEventListener('click', openModelStudio);

  const closeModelStudioBtn = el('closeModelStudio');
  if (closeModelStudioBtn) closeModelStudioBtn.addEventListener('click', closeModelStudio);

  const modelStudioForm = el('modelStudioForm');
  if (modelStudioForm) modelStudioForm.addEventListener('submit', saveModelConfig);

  const probeModelsBtn = el('probeModelsBtn');
  if (probeModelsBtn) probeModelsBtn.addEventListener('click', () => probeModels(true));

  const testModelBtn = el('testModelBtn');
  if (testModelBtn) testModelBtn.addEventListener('click', testModelConnection);

  const primaryModelSelect = el('primaryModelSelect');
  if (primaryModelSelect) {
    primaryModelSelect.addEventListener('change', (e) => {
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

  const launchpadModelSelect = el('launchpadModelSelect');
  if (launchpadModelSelect) {
    launchpadModelSelect.addEventListener('change', async (e) => {
      const selectedModel = e.target.value;
      if (!selectedModel || !state.modelConfig) return;
      try {
        const payload = {
          ...state.modelConfig,
          primary_model: selectedModel,
          api_key: '', // Retain existing secret
        };
        const updated = await api('/api/v1/models/config', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        state.modelConfig = updated;
        updateModelBadge(updated);
        showToast(`✓ Active Model switched to: ${selectedModel}`);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  // Event Search Input
  const eventSearchInput = el('eventSearchInput');
  if (eventSearchInput) {
    eventSearchInput.addEventListener('input', (e) => {
      state.searchQuery = e.target.value;
      renderEvents(state.events);
    });
  }

  // Modal Closers
  el('closeDrawer').addEventListener('click', () => el('taskDrawer').close());
  el('closeArtifactModal').addEventListener('click', () => el('artifactDialog').close());

  // Timeline Filter Pills
  el('timelineFilters').addEventListener('click', (e) => {
    if (e.target.tagName === 'BUTTON') {
      document.querySelectorAll('#timelineFilters .pill').forEach(p => p.classList.remove('active'));
      e.target.classList.add('active');
      state.currentFilter = e.target.dataset.filter || 'ALL';
      renderEvents(state.events);
    }
  });

  // Global Keyboard Shortcuts
  window.addEventListener('keydown', (e) => {
    const isInputActive = document.activeElement && 
      (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA');

    if ((e.key === '/' || (e.key === 'k' && (e.metaKey || e.ctrlKey))) && document.activeElement !== el('runLookup')) {
      e.preventDefault();
      el('runLookup').focus();
      el('runLookup').select();
    }
    if (e.key === 'Escape') {
      if (el('taskDrawer').open) el('taskDrawer').close();
      if (el('artifactDialog').open) el('artifactDialog').close();
      if (el('authDialog').open) el('authDialog').close();
      if (el('modelStudioDialog').open) el('modelStudioDialog').close();
    }
    if (!isInputActive) {
      if (e.key === '1') {
        document.querySelectorAll('#timelineFilters .pill')[0]?.click();
      } else if (e.key === '2') {
        document.querySelectorAll('#timelineFilters .pill')[1]?.click();
      } else if (e.key === '3') {
        document.querySelectorAll('#timelineFilters .pill')[2]?.click();
      } else if (e.key === '4') {
        document.querySelectorAll('#timelineFilters .pill')[3]?.click();
      }
    }
  });

  window.addEventListener('resize', () => {
    if (state.tasks.length) drawDAGConnectors(state.tasks);
  });

  // Event Listeners & Binding
  el('openAuth').addEventListener('click', openAuth);

  el('clearToken').addEventListener('click', () => {
    clearToken();
    el('adminToken').value = '';
    el('authBtnText').textContent = 'Admin Access';
    showToast('Session token cleared.');
    closeAuth();
  });

  el('authForm').addEventListener('submit', () => {
    setToken(el('adminToken').value.trim());
    el('authBtnText').textContent = 'Admin Connected';
    showToast('Admin token saved for this session.');
    closeAuth();
    if (state.runId) void loadRun(state.runId);
  });

  el('browseFolderBtn').addEventListener('click', selectDirectory);
  el('dirPickerFallback').addEventListener('change', handleFallbackDirPicker);
  el('projectForm').addEventListener('submit', registerProject);
  el('runForm').addEventListener('submit', startRun);

  el('lookupForm').addEventListener('submit', (e) => {
    e.preventDefault();
    void loadRun(el('runLookup').value);
  });

  el('refreshRun').addEventListener('click', () => void loadRun(state.runId));

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

  // Fill Sample SHA Helper Button
  const fillSampleShaBtn = el('fillSampleSha');
  if (fillSampleShaBtn) {
    fillSampleShaBtn.addEventListener('click', () => {
      el('baselineCommit').value = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';
      showToast('Filled sample commit SHA.');
    });
  }

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
  function showLaunchpad() {
    hideRunsBrowser();
    el('dashboard')?.classList.add('hidden');
    el('launchpadSection')?.classList.remove('hidden');
    document.querySelectorAll('[data-nav]').forEach((tab) => {
      tab.classList.toggle('active', tab.dataset.nav === 'new');
    });
  }

  function backToRuns() {
    el('dashboard')?.classList.add('hidden');
    el('launchpadSection')?.classList.add('hidden');
    showRunsBrowser();
  }

  function paletteActions() {
    const items = [
      { id: 'runs', title: 'Go to Runs Browser', icon: '▦', hint: 'overview', keywords: 'runs list missions browse', run: backToRuns },
      { id: 'new', title: 'New Mission', icon: '＋', hint: 'launch', keywords: 'new run launch start goal create', run: showLaunchpad },
    ];
    if (state.runId) {
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
      items.push({
        id: 'cancel',
        title: 'Cancel Current Run',
        icon: '✕',
        hint: 'destructive',
        keywords: 'cancel abort stop mission',
        run: async () => {
          try {
            await api(`/api/v1/runs/${encodeURIComponent(state.runId)}/cancel`, { method: 'POST' });
            showToast('Cancellation requested — workers will wind down.');
          } catch (error) {
            showToast(error.message, true);
          }
        },
      });
      if (state.projectId && state.tasks.length) {
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
        id: `run:${recent.runId}`,
        title: `Open ${recent.goal || recent.runId}`.slice(0, 80),
        icon: '▸',
        hint: recent.runId.slice(0, 8),
        keywords: `open run mission ${recent.runId}`,
        run: () => { void loadRun(recent.runId); },
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

  // Bootstrap
  el('runsNewBtn')?.addEventListener('click', showLaunchpad);
  initPalette(paletteActions);
  setUnauthorizedHandler(openAuth);
  initRunsBrowser({
    onOpenRun: (runId) => { void loadRun(runId); },
    onNewRun: showLaunchpad,
  });
  initTour();
  void checkHealth();
  window.setInterval(checkHealth, 15000);
  restoreIdentity();
  void loadModelConfig();
  if (!state.runId) {
    backToRuns();
  }
})();
