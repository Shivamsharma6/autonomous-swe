(() => {
  'use strict';

  // Application Global State
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
    pollTimer: null,
    ws: null,
    wsReconnectTimer: null,
    selectedTask: null,
  };

  const el = (id) => document.getElementById(id);
  const terminalStates = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);

  // Centralized Authenticated API Client
  async function api(path, options = {}) {
    if (!state.token) {
      openAuth();
      throw new Error('Operator authentication is required.');
    }
    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      openAuth();
      throw new Error('Admin token was rejected by server.');
    }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        if (body.detail) {
          message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
        }
      } catch (_) { /* non-json response */ }
      throw new Error(message);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  // Toast Notification Manager
  function showToast(message, isError = false) {
    const toast = el('toast');
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.remove('hidden');
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.add('hidden'), 4000);
  }

  // Authentication Dialog Controls
  function openAuth() {
    el('adminToken').value = state.token;
    if (!el('authDialog').open) el('authDialog').showModal();
  }

  function closeAuth() {
    if (el('authDialog').open) el('authDialog').close();
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
    if (state.token) {
      el('authBtnText').textContent = 'Admin Connected';
    }
    if (state.projectId && state.repositoryId) {
      el('projectIdentity').textContent = `${state.projectName || 'Project'} · ${state.projectId}`;
      el('startRun').disabled = false;
    }
    if (state.runId) {
      el('runLookup').value = state.runId;
      void loadRun(state.runId);
    }
    if (!state.token) {
      window.setTimeout(openAuth, 300);
    }
  }

  // Handle File / Directory Picker for Local Repository Selection
  async function selectDirectory(event) {
    if (event) {
      event.preventDefault();
    }
    if (window.showDirectoryPicker) {
      try {
        const dirHandle = await window.showDirectoryPicker({ mode: 'read' });
        if (!dirHandle) return;
        const dirName = dirHandle.name;
        el('projectName').value = dirName;
        el('sourcePath').value = `/var/lib/autoswe/imports/${dirName}`;

        try {
          const gitHandle = await dirHandle.getDirectoryHandle('.git');
          if (gitHandle) {
            const headHandle = await gitHandle.getFileHandle('HEAD');
            const headFile = await headHandle.getFile();
            const headText = (await headFile.text()).trim();

            if (headText.startsWith('ref: refs/heads/')) {
              const branch = headText.replace('ref: refs/heads/', '').trim();
              el('defaultBranch').value = branch;

              try {
                const refsHandle = await gitHandle.getDirectoryHandle('refs');
                const headsHandle = await refsHandle.getDirectoryHandle('heads');
                const branchHandle = await headsHandle.getFileHandle(branch);
                const branchFile = await branchHandle.getFile();
                const commitSha = (await branchFile.text()).trim();
                if (commitSha && commitSha.length >= 40) {
                  el('baselineCommit').value = commitSha;
                  showToast(`Selected "${dirName}" (${branch} · ${commitSha.slice(0, 8)})`);
                  return;
                }
              } catch (_) {}
              showToast(`Selected "${dirName}" (Branch: ${branch})`);
              return;
            } else if (headText.length >= 40) {
              el('baselineCommit').value = headText;
              showToast(`Selected "${dirName}" (Commit: ${headText.slice(0, 8)})`);
              return;
            }
          }
        } catch (_) {}
        showToast(`Selected repository directory: ${dirName}`);
        return;
      } catch (err) {
        if (err.name === 'AbortError') return;
        console.warn('showDirectoryPicker unavailable, falling back to input:', err);
      }
    }

    const fallbackInput = el('dirPickerFallback');
    if (fallbackInput) {
      fallbackInput.value = '';
      fallbackInput.click();
    }
  }

  function handleFallbackDirPicker(event) {
    const files = event.target.files;
    if (!files || !files.length) return;
    const firstFile = files[0];
    const pathParts = (firstFile.webkitRelativePath || '').split('/');
    if (pathParts.length > 1) {
      const dirName = pathParts[0];
      el('projectName').value = dirName;
      el('sourcePath').value = `/var/lib/autoswe/imports/${dirName}`;
      
      let headFile = null;
      for (let i = 0; i < files.length; i++) {
        if (files[i].webkitRelativePath === `${dirName}/.git/HEAD`) {
          headFile = files[i];
          break;
        }
      }

      if (headFile) {
        const reader = new FileReader();
        reader.onload = (e) => {
          const text = (e.target.result || '').trim();
          if (text.startsWith('ref: refs/heads/')) {
            const branch = text.replace('ref: refs/heads/', '').trim();
            el('defaultBranch').value = branch;

            for (let j = 0; j < files.length; j++) {
              if (files[j].webkitRelativePath === `${dirName}/.git/refs/heads/${branch}`) {
                const refReader = new FileReader();
                refReader.onload = (re) => {
                  const commit = (re.target.result || '').trim();
                  if (commit && commit.length >= 40) {
                    el('baselineCommit').value = commit;
                    showToast(`Selected "${dirName}" (${branch} · ${commit.slice(0, 8)})`);
                  }
                };
                refReader.readAsText(files[j]);
                return;
              }
            }
            showToast(`Selected "${dirName}" (Branch: ${branch})`);
          } else if (text.length >= 40) {
            el('baselineCommit').value = text;
            showToast(`Selected "${dirName}" (Commit: ${text.slice(0, 8)})`);
          }
        };
        reader.readAsText(headFile);
      } else {
        showToast(`Selected folder: ${dirName}`);
      }
    }
  }

  // Register New Project Repository
  async function registerProject(event) {
    event.preventDefault();
    try {
      const body = await api('/api/v1/projects', {
        method: 'POST',
        body: JSON.stringify({
          name: el('projectName').value.trim(),
          source_path: el('sourcePath').value.trim(),
          default_branch: el('defaultBranch').value.trim(),
        }),
      });
      state.projectId = body.project_id;
      state.repositoryId = body.repository_id;
      state.projectName = el('projectName').value.trim();
      sessionStorage.setItem('autoswe.projectId', state.projectId);
      sessionStorage.setItem('autoswe.repositoryId', state.repositoryId);
      sessionStorage.setItem('autoswe.projectName', state.projectName);
      el('projectIdentity').textContent = `${state.projectName} · ${state.projectId}`;
      el('startRun').disabled = false;
      showToast('Repository registered inside governed import boundary.');
    } catch (error) {
      showToast(error.message, true);
    }
  }

  // Start Autonomous Run Workflow
  async function startRun(event) {
    event.preventDefault();
    const runBtn = el('startRun');
    const originalText = runBtn.innerHTML;
    runBtn.disabled = true;

    try {
      // Auto-register project repository if not registered yet in this session
      if (!state.projectId || !state.repositoryId) {
        showToast('Registering repository...');
        const projBody = await api('/api/v1/projects', {
          method: 'POST',
          body: JSON.stringify({
            name: el('projectName').value.trim(),
            source_path: el('sourcePath').value.trim(),
            default_branch: el('defaultBranch').value.trim(),
          }),
        });
        state.projectId = projBody.project_id;
        state.repositoryId = projBody.repository_id;
        state.projectName = el('projectName').value.trim();
        sessionStorage.setItem('autoswe.projectId', state.projectId);
        sessionStorage.setItem('autoswe.repositoryId', state.repositoryId);
        sessionStorage.setItem('autoswe.projectName', state.projectName);
        el('projectIdentity').textContent = `${state.projectName} · ${state.projectId}`;
      }

      showToast('Launching agentic run...');
      const body = await api('/api/v1/runs', {
        method: 'POST',
        body: JSON.stringify({
          project_id: state.projectId,
          repository_id: state.repositoryId,
          goal: el('runGoal').value.trim(),
          baseline_commit: el('baselineCommit').value.trim().toLowerCase(),
        }),
      });
      state.runId = body.run_id;
      sessionStorage.setItem('autoswe.runId', state.runId);
      el('runLookup').value = state.runId;
      showToast('Run launched. Architect agent is synthesizing the execution DAG.');
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

  // WebSocket Live Event Streaming with Token Auth
  function setupWebSocket(projectId, runId) {
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    window.clearTimeout(state.wsReconnectTimer);

    // If there's an active running task, stream from task WebSocket endpoint
    const runningTask = state.tasks.find(t => t.state === 'RUNNING' || t.state === 'LEASED');
    if (!runningTask) {
      el('streamStatusText').textContent = 'POLLING ACTIVE';
      return;
    }

    try {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/api/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(runningTask.id)}/events?token=${encodeURIComponent(state.token)}`;
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        el('liveStreamBadge').classList.add('live');
        el('streamStatusText').textContent = 'LIVE WEBSOCKET';
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          state.events.unshift(payload);
          renderEvents(state.events);
        } catch (_) {}
      };

      ws.onerror = () => {
        el('streamStatusText').textContent = 'POLLING FALLBACK';
        el('liveStreamBadge').classList.remove('live');
      };

      ws.onclose = () => {
        state.ws = null;
      };

      state.ws = ws;
    } catch (_) {
      el('streamStatusText').textContent = 'POLLING ACTIVE';
    }
  }

  // Render Dashboard
  function renderRun(run) {
    el('dashboard').classList.remove('hidden');
    el('runGoalTitle').textContent = run.goal;
    el('runIdText').textContent = run.run_id;
    el('runProjectName').textContent = state.projectName || run.project_id.slice(0, 8);
    
    // Status Badge
    const statusBadge = el('runStatusBadge');
    statusBadge.textContent = run.state;
    statusBadge.className = `status-pill ${run.state.toLowerCase()}`;

    // Metrics
    el('runState').textContent = run.state;
    el('stateDuration').textContent = `${formatDuration(run.state_duration_seconds)} in current state`;
    el('planRevision').textContent = run.active_plan_revision === null ? 'Planning' : `r${run.active_plan_revision}`;
    el('taskSummary').textContent = summarizeCounts(run.task_counts);

    const totalTokens = (run.model_input_tokens || 0) + (run.model_output_tokens || 0);
    el('tokenTotal').textContent = totalTokens.toLocaleString();
    el('tokenDetail').textContent = `${(run.model_input_tokens || 0).toLocaleString()} in · ${(run.model_output_tokens || 0).toLocaleString()} out`;
    el('modelCost').textContent = `$${Number(run.model_cost_usd || 0).toFixed(4)}`;

    renderDAG(state.tasks);
    renderApprovals(state.approvals);
    renderArtifacts(state.artifacts, run.project_id);
    renderEvents(state.events);
  }

  // Topological DAG Layout & Column Grouping Engine
  function renderDAG(tasks) {
    const root = el('taskDag');
    const svg = el('dagSvgConnections');
    root.replaceChildren();
    svg.replaceChildren();

    if (!tasks || !tasks.length) {
      root.innerHTML = '<div class="empty-state">Waiting for the Architect agent to synthesize execution DAG...</div>';
      return;
    }

    // Compute topological ranks / levels for each task
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

    // Group tasks by calculated level
    const maxRank = Math.max(...Array.from(ranks.values()), 0);
    const columns = Array.from({ length: maxRank + 1 }, () => []);

    tasks.forEach(t => {
      const rank = ranks.get(t.id) || 0;
      columns[rank].push(t);
    });

    const levelNames = ['Stage 1: Discovery & Research', 'Stage 2: Implementation', 'Stage 3: Verification & Test', 'Stage 4: Validation & Review', 'Stage 5: Finalization'];

    // Render Columns & Nodes
    columns.forEach((columnTasks, levelIdx) => {
      const colEl = document.createElement('div');
      colEl.className = 'dag-column';

      const colHeader = document.createElement('div');
      colHeader.className = 'dag-column-header';
      colHeader.textContent = levelNames[levelIdx] || `Stage ${levelIdx + 1}`;
      colEl.append(colHeader);

      columnTasks.forEach(task => {
        const node = document.createElement('article');
        node.className = `task-node ${task.state.toLowerCase()}`;
        node.id = `node-${task.id}`;
        node.dataset.taskId = task.id;

        const header = document.createElement('div');
        header.className = 'task-node-header';
        
        const typeTag = document.createElement('span');
        typeTag.className = 'task-type-tag';
        typeTag.textContent = task.task_type;

        const statusPill = document.createElement('span');
        statusPill.className = `status-pill ${task.state.toLowerCase()}`;
        statusPill.textContent = task.state;
        header.append(typeTag, statusPill);

        const title = document.createElement('h4');
        title.className = 'task-node-title';
        title.textContent = task.title;

        const meta = document.createElement('div');
        meta.className = 'task-node-meta';
        const depsCount = task.dependencies.length;
        meta.innerHTML = `<span>${task.assigned_capability}</span><span>${depsCount ? `${depsCount} dep${depsCount > 1 ? 's' : ''}` : 'Root task'}</span>`;

        node.append(header, title, meta);
        node.addEventListener('click', () => openTaskDrawer(task));
        colEl.append(node);
      });

      root.append(colEl);
    });

    // Draw SVG dependency curves after layout rendering
    window.requestAnimationFrame(() => drawDAGConnectors(tasks));
  }

  // Draw Smooth Bezier Connections between Dependency Nodes
  function drawDAGConnectors(tasks) {
    const svg = el('dagSvgConnections');
    const viewport = el('dagViewport');
    if (!svg || !viewport) return;

    svg.replaceChildren();
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

        // Calculate connection coordinates relative to scrollable viewport
        const startX = parentRect.right - viewportRect.left + viewport.scrollLeft;
        const startY = parentRect.top + (parentRect.height / 2) - viewportRect.top + viewport.scrollTop;
        const endX = childRect.left - viewportRect.left + viewport.scrollLeft;
        const endY = childRect.top + (childRect.height / 2) - viewportRect.top + viewport.scrollTop;

        const dx = Math.max(Math.abs(endX - startX) * 0.5, 30);
        const pathData = `M ${startX} ${startY} C ${startX + dx} ${startY}, ${endX - dx} ${endY}, ${endX} ${endY}`;

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', pathData);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', '#2a3e56');
        path.setAttribute('stroke-width', '1.8');
        path.setAttribute('stroke-dasharray', '4 2');
        svg.append(path);
      });
    });
  }

  // Task Detail Drawer
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
    el('drawerTaskDeps').textContent = task.dependencies.length ? task.dependencies.join(', ') : 'None (Root)';
    el('drawerTaskRevision').textContent = `r${task.plan_revision}`;

    el('drawerTaskGoal').textContent = task.goal || task.title;

    // Filter events associated with this task
    const taskEvents = state.events.filter(e => e.payload && e.payload.task_id === task.id);
    const eventsContainer = el('drawerTaskEvents');
    eventsContainer.replaceChildren();

    if (!taskEvents.length) {
      eventsContainer.innerHTML = '<p class="empty-text">No direct events recorded for this task yet.</p>';
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

  // Artifact Preview & Download
  async function previewArtifact(projectId, artifact) {
    el('modalArtifactType').textContent = artifact.media_type;
    el('modalArtifactTitle').textContent = `Artifact ${artifact.artifact_id.slice(0, 8)}`;
    el('modalArtifactMeta').textContent = `SHA-256: ${artifact.sha256} • Size: ${formatBytes(artifact.size_bytes)}`;
    el('artifactPreviewCode').textContent = 'Fetching and verifying object content...';
    el('downloadArtifactBtn').onclick = () => downloadArtifact(projectId, artifact);
    el('artifactDialog').showModal();

    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${state.token}` },
      });
      if (!response.ok) throw new Error(`Fetch failed (${response.status})`);
      const text = await response.text();
      el('artifactPreviewCode').textContent = text || '(Empty content)';
    } catch (err) {
      el('artifactPreviewCode').textContent = `Failed to preview artifact: ${err.message}`;
    }
  }

  async function downloadArtifact(projectId, artifact) {
    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${state.token}` },
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

  // Render Immutable Audit Events Timeline
  function renderEvents(events) {
    const root = el('eventList');
    root.replaceChildren();

    const filtered = (events || []).filter(e => {
      if (state.currentFilter === 'ALL') return true;
      if (state.currentFilter === 'TASK') return e.event_type.startsWith('task.');
      if (state.currentFilter === 'TOOL') return e.event_type.startsWith('tool.');
      if (state.currentFilter === 'APPROVAL') return e.event_type.startsWith('approval.');
      return true;
    });

    if (!filtered.length) {
      root.innerHTML = '<li class="empty-state">No matching events in audit timeline.</li>';
      return;
    }

    filtered.forEach(event => {
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
      data.textContent = JSON.stringify(event.payload);

      item.append(time, name, data);
      root.append(item);
    });
  }

  // Utility Formatters
  function summarizeCounts(counts) {
    const entries = Object.entries(counts || {});
    if (!entries.length) return 'No tasks';
    return entries.map(([name, count]) => `${count} ${name.toLowerCase()}`).join(' · ');
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    if (value < 60) return `${value}s`;
    if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
    return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1048576) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1048576).toFixed(1)} MiB`;
  }

  // Event Listeners & Binding
  el('openAuth').addEventListener('click', openAuth);
  
  el('clearToken').addEventListener('click', () => {
    state.token = '';
    sessionStorage.removeItem('autoswe.adminToken');
    el('adminToken').value = '';
    el('authBtnText').textContent = 'Admin Access';
    showToast('Session token cleared.');
    closeAuth();
  });

  el('authForm').addEventListener('submit', () => {
    state.token = el('adminToken').value.trim();
    sessionStorage.setItem('autoswe.adminToken', state.token);
    el('authBtnText').textContent = 'Admin Connected';
    showToast('Admin token saved for this browser session.');
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
    await navigator.clipboard.writeText(state.runId);
    showToast('Run ID copied to clipboard.');
  });

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
    if (e.key === '/' && document.activeElement !== el('runLookup') && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault();
      el('runLookup').focus();
    }
    if (e.key === 'Escape') {
      if (el('taskDrawer').open) el('taskDrawer').close();
      if (el('artifactDialog').open) el('artifactDialog').close();
      if (el('authDialog').open) el('authDialog').close();
    }
  });

  window.addEventListener('resize', () => {
    if (state.tasks.length) drawDAGConnectors(state.tasks);
  });

  // Bootstrap
  void checkHealth();
  window.setInterval(checkHealth, 15000);
  restoreIdentity();
})();
