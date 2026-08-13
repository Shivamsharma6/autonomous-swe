(() => {
  'use strict';

  const state = {
    token: sessionStorage.getItem('autoswe.adminToken') || '',
    projectId: sessionStorage.getItem('autoswe.projectId') || '',
    repositoryId: sessionStorage.getItem('autoswe.repositoryId') || '',
    runId: sessionStorage.getItem('autoswe.runId') || '',
    projectName: sessionStorage.getItem('autoswe.projectName') || '',
    pollTimer: null,
  };

  const el = (id) => document.getElementById(id);
  const terminalStates = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);

  async function api(path, options = {}) {
    if (!state.token) throw new Error('Admin access is required.');
    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      openAuth();
      throw new Error('Admin token was rejected.');
    }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        if (body.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      } catch (_) { /* response did not contain JSON */ }
      throw new Error(message);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function showToast(message, error = false) {
    const toast = el('toast');
    toast.textContent = message;
    toast.classList.toggle('error', error);
    toast.classList.remove('hidden');
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.add('hidden'), 4200);
  }

  function openAuth() {
    el('adminToken').value = state.token;
    if (!el('authDialog').open) el('authDialog').showModal();
  }

  async function checkHealth() {
    try {
      const response = await fetch('/health/ready');
      const body = await response.json();
      const ready = response.ok && body.ready;
      el('healthDot').className = `dot ${ready ? 'ready' : 'failed'}`;
      const unavailable = Object.entries(body.dependencies || {}).filter(([, value]) => !value).map(([name]) => name);
      el('healthText').textContent = ready ? 'Platform ready' : `Degraded: ${unavailable.join(', ') || 'dependencies'}`;
    } catch (_) {
      el('healthDot').className = 'dot failed';
      el('healthText').textContent = 'Control plane unavailable';
    }
  }

  function restoreIdentity() {
    if (state.projectId && state.repositoryId) {
      el('projectIdentity').textContent = `${state.projectName || 'Project'} · ${state.projectId} · repo ${state.repositoryId}`;
      el('startRun').disabled = false;
    }
    if (state.runId) {
      el('runLookup').value = state.runId;
      void loadRun(state.runId);
    }
    if (!state.token) window.setTimeout(openAuth, 250);
  }

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
      el('projectIdentity').textContent = `${state.projectName} · ${state.projectId} · repo ${state.repositoryId}`;
      el('startRun').disabled = false;
      showToast('Repository registered inside the governed import boundary.');
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function startRun(event) {
    event.preventDefault();
    if (!state.projectId || !state.repositoryId) return showToast('Register a project first.', true);
    try {
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
      showToast('Run accepted. The architect will produce the first plan revision.');
      await loadRun(state.runId);
    } catch (error) {
      showToast(error.message, true);
    }
  }

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
      renderRun(run, tasks, approvals, artifacts, events);
      if (!terminalStates.has(run.state)) {
        state.pollTimer = window.setTimeout(() => void loadRun(candidate), 2500);
      }
    } catch (error) {
      el('pollStatus').textContent = 'Paused';
      showToast(error.message, true);
    }
  }

  function renderRun(run, tasks, approvals, artifacts, events) {
    el('dashboard').classList.remove('hidden');
    el('runGoalTitle').textContent = run.goal;
    el('runIdText').textContent = run.run_id;
    el('runState').textContent = run.state;
    el('stateDuration').textContent = `${formatDuration(run.state_duration_seconds)} in current state`;
    el('planRevision').textContent = run.active_plan_revision === null ? 'Planning' : `r${run.active_plan_revision}`;
    el('taskSummary').textContent = summarizeCounts(run.task_counts);
    const totalTokens = run.model_input_tokens + run.model_output_tokens;
    el('tokenTotal').textContent = totalTokens.toLocaleString();
    el('tokenDetail').textContent = `${run.model_input_tokens.toLocaleString()} in · ${run.model_output_tokens.toLocaleString()} out`;
    el('modelCost').textContent = `$${Number(run.model_cost_usd).toFixed(4)}`;
    el('pollStatus').textContent = terminalStates.has(run.state) ? 'Terminal' : 'Live';
    renderTasks(tasks);
    renderApprovals(approvals);
    renderArtifacts(artifacts, run.project_id);
    renderEvents(events);
  }

  function renderTasks(tasks) {
    const root = el('taskDag');
    root.replaceChildren();
    root.classList.toggle('empty', tasks.length === 0);
    if (!tasks.length) {
      root.textContent = 'Waiting for the architect to persist a plan.';
      return;
    }
    for (const task of tasks) {
      const card = document.createElement('article');
      card.className = `task-card ${task.state.toLowerCase()}`;
      const top = document.createElement('div');
      top.className = 'task-top';
      const title = document.createElement('strong');
      title.textContent = task.title;
      title.title = task.title;
      const badge = document.createElement('span');
      badge.className = 'state-badge';
      badge.textContent = task.state;
      top.append(title, badge);
      const meta = document.createElement('p');
      meta.className = 'task-meta';
      const dependencies = task.dependencies.length ? task.dependencies.map((value) => value.slice(0, 8)).join(', ') : 'root';
      meta.textContent = `${task.task_type} · ${task.assigned_capability} · r${task.plan_revision}\ndepends: ${dependencies}`;
      meta.style.whiteSpace = 'pre-line';
      card.append(top, meta);
      root.append(card);
    }
  }

  function renderApprovals(approvals) {
    const root = el('approvalList');
    root.replaceChildren();
    root.classList.toggle('empty', approvals.length === 0);
    if (!approvals.length) {
      root.textContent = 'No approvals.';
      return;
    }
    for (const approval of approvals) {
      const item = document.createElement('article');
      item.className = 'stack-item';
      const title = document.createElement('strong');
      title.textContent = `${approval.tool_name} · ${approval.status}`;
      const hash = document.createElement('code');
      hash.textContent = `call ${approval.call_hash}`;
      const expiry = document.createElement('small');
      expiry.textContent = `expires ${new Date(approval.expires_at).toLocaleString()}`;
      item.append(title, hash, expiry);
      if (approval.status === 'PENDING') {
        const actions = document.createElement('div');
        actions.className = 'approval-actions';
        const reject = button('Reject', 'button ghost', () => decideApproval(approval, false));
        const approve = button('Approve exact call', 'button primary', () => decideApproval(approval, true));
        actions.append(reject, approve);
        item.append(actions);
      }
      root.append(item);
    }
  }

  async function decideApproval(approval, approved) {
    const approver = window.prompt('Operator identity for the immutable audit record:');
    if (!approver || !approver.trim()) return;
    try {
      await api(`/api/v1/approvals/${encodeURIComponent(approval.approval_id)}/decision`, {
        method: 'POST',
        body: JSON.stringify({ approved, approver: approver.trim(), expected_call_hash: approval.call_hash }),
      });
      showToast(approved ? 'Exact tool call approved.' : 'Tool call rejected.');
      await loadRun(state.runId);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function renderArtifacts(artifacts, projectId) {
    const root = el('artifactList');
    root.replaceChildren();
    root.classList.toggle('empty', artifacts.length === 0);
    if (!artifacts.length) {
      root.textContent = 'No evidence yet.';
      return;
    }
    for (const artifact of artifacts) {
      const item = document.createElement('article');
      item.className = 'stack-item';
      const link = button(artifact.media_type, 'artifact-link', () => downloadArtifact(projectId, artifact));
      const hash = document.createElement('code');
      hash.textContent = `sha256:${artifact.sha256}`;
      const detail = document.createElement('small');
      detail.textContent = `${artifact.state} · ${formatBytes(artifact.size_bytes)} · ${artifact.artifact_id}`;
      item.append(link, hash, detail);
      root.append(item);
    }
  }

  async function downloadArtifact(projectId, artifact) {
    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, {
        headers: { Authorization: `Bearer ${state.token}` },
      });
      if (!response.ok) throw new Error(`Artifact verification failed (${response.status}).`);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.artifact_id;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function renderEvents(events) {
    const root = el('eventList');
    root.replaceChildren();
    root.classList.toggle('empty', events.length === 0);
    if (!events.length) {
      const item = document.createElement('li');
      item.textContent = 'No events yet.';
      root.append(item);
      return;
    }
    for (const event of [...events].reverse()) {
      const item = document.createElement('li');
      const time = document.createElement('span');
      time.textContent = new Date(event.created_at).toLocaleString();
      const name = document.createElement('span');
      name.className = 'event-name';
      name.textContent = event.event_type;
      const data = document.createElement('span');
      data.className = 'event-data';
      data.textContent = JSON.stringify(event.payload);
      item.append(time, name, data);
      root.append(item);
    }
  }

  function button(label, className, handler) {
    const result = document.createElement('button');
    result.type = 'button';
    result.className = className;
    result.textContent = label;
    result.addEventListener('click', handler);
    return result;
  }

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

  el('openAuth').addEventListener('click', openAuth);
  el('clearToken').addEventListener('click', () => {
    state.token = '';
    sessionStorage.removeItem('autoswe.adminToken');
    el('adminToken').value = '';
    showToast('Session token cleared.');
  });
  el('authForm').addEventListener('submit', () => {
    state.token = el('adminToken').value.trim();
    sessionStorage.setItem('autoswe.adminToken', state.token);
    showToast('Admin access saved for this browser session.');
    if (state.runId) window.setTimeout(() => void loadRun(state.runId), 0);
  });
  el('projectForm').addEventListener('submit', registerProject);
  el('runForm').addEventListener('submit', startRun);
  el('lookupForm').addEventListener('submit', (event) => {
    event.preventDefault();
    void loadRun(el('runLookup').value);
  });
  el('refreshRun').addEventListener('click', () => void loadRun(state.runId));
  el('copyRunId').addEventListener('click', async () => {
    await navigator.clipboard.writeText(state.runId);
    showToast('Run ID copied.');
  });

  void checkHealth();
  window.setInterval(checkHealth, 15000);
  restoreIdentity();
})();
