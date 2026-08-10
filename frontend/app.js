/**
 * Autonomous SWE Platform - OpenCode & Codex Inspired Control Plane Client
 */

(function () {
    'use strict';

    // State Variables
    let currentTaskId = null;
    let wsSocket = null;
    let timerInterval = null;
    let startTime = null;
    let totalTokens = 0;
    let promptTokens = 0;
    let completionTokens = 0;
    let activeDiffFiles = {}; // filename -> content
    let currentActiveDiffFile = null;
    let activeTraceFilter = 'ALL';
    let traceSearchQuery = '';
    let autoScrollLogs = true;

    const STAGES = ['architect', 'researcher', 'coder', 'tester', 'sandbox', 'reviewer'];

    // DOM References
    const dom = {
        // Inputs & Buttons
        taskPromptInput: document.getElementById('taskPromptInput'),
        submitTaskBtn: document.getElementById('submitTaskBtn'),
        demoTaskBtn: document.getElementById('demoTaskBtn'),
        providerConfigBtn: document.getElementById('providerConfigBtn'),
        activeModelBadge: document.getElementById('activeModelBadge'),
        activeModelText: document.getElementById('activeModelText'),

        // Status Badges
        wsStatusBadge: document.getElementById('wsStatusBadge'),
        wsStatusDot: document.getElementById('wsStatusDot'),
        wsStatusText: document.getElementById('wsStatusText'),

        // Metrics
        metricStatusPill: document.getElementById('metricStatusPill'),
        metricTaskId: document.getElementById('metricTaskId'),
        metricTaskTitle: document.getElementById('metricTaskTitle'),
        metricRuntime: document.getElementById('metricRuntime'),
        metricTokens: document.getElementById('metricTokens'),
        metricTokensSub: document.getElementById('metricTokensSub'),
        metricAdditions: document.getElementById('metricAdditions'),
        metricDeletions: document.getElementById('metricDeletions'),
        metricFilesChanged: document.getElementById('metricFilesChanged'),
        metricProgressText: document.getElementById('metricProgressText'),
        metricProgressPercent: document.getElementById('metricProgressPercent'),
        metricProgressBar: document.getElementById('metricProgressBar'),

        // DAG Pipeline Nodes
        activeAgentStageTag: document.getElementById('activeAgentStageTag'),

        // Code Diff Viewer
        diffTabsHeader: document.getElementById('diffTabsHeader'),
        diffFilePath: document.getElementById('diffFilePath'),
        diffPlaceholder: document.getElementById('diffPlaceholder'),
        diffCodeWrapper: document.getElementById('diffCodeWrapper'),
        diffContentPre: document.getElementById('diffContentPre'),
        copyDiffBtn: document.getElementById('copyDiffBtn'),

        // Trace Stream
        traceStreamContainer: document.getElementById('traceStreamContainer'),
        traceEntriesList: document.getElementById('traceEntriesList'),
        traceSearchInput: document.getElementById('traceSearchInput'),
        traceFilterType: document.getElementById('traceFilterType'),
        clearLogsBtn: document.getElementById('clearLogsBtn'),
        autoScrollToggle: document.getElementById('autoScrollToggle'),

        // Modal Elements
        providerModal: document.getElementById('providerModal'),
        closeModalBtn: document.getElementById('closeModalBtn'),
        providerSelect: document.getElementById('providerSelect'),
        baseUrlInput: document.getElementById('baseUrlInput'),
        baseUrlHint: document.getElementById('baseUrlHint'),
        apiKeyInput: document.getElementById('apiKeyInput'),
        modelNameInput: document.getElementById('modelNameInput'),
        modelDropdownSelect: document.getElementById('modelDropdownSelect'),
        fetchModelsBtn: document.getElementById('fetchModelsBtn'),
        tempInput: document.getElementById('tempInput'),
        tempValue: document.getElementById('tempValue'),
        testProviderBtn: document.getElementById('testProviderBtn'),
        saveProviderBtn: document.getElementById('saveProviderBtn'),
        connectionStatusBox: document.getElementById('connectionStatusBox'),
    };

    // Initialize Client
    function init() {
        bindEvents();
        loadActiveProviderConfig();
        resetDashboardState();
    }

    // Event Bounding
    function bindEvents() {
        if (dom.submitTaskBtn) dom.submitTaskBtn.addEventListener('click', handleTaskSubmit);
        if (dom.taskPromptInput) {
            dom.taskPromptInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') handleTaskSubmit();
            });
        }
        if (dom.demoTaskBtn) dom.demoTaskBtn.addEventListener('click', handleDemoTaskSubmit);

        // Modal Controls
        if (dom.providerConfigBtn) dom.providerConfigBtn.addEventListener('click', () => showModal(true));
        if (dom.closeModalBtn) dom.closeModalBtn.addEventListener('click', () => showModal(false));
        if (dom.providerSelect) dom.providerSelect.addEventListener('change', handleProviderSelectChange);
        if (dom.tempInput) dom.tempInput.addEventListener('input', (e) => dom.tempValue.textContent = e.target.value);
        if (dom.fetchModelsBtn) dom.fetchModelsBtn.addEventListener('click', fetchInstalledModels);
        if (dom.modelDropdownSelect) {
            dom.modelDropdownSelect.addEventListener('change', (e) => {
                if (e.target.value) dom.modelNameInput.value = e.target.value;
            });
        }
        if (dom.testProviderBtn) dom.testProviderBtn.addEventListener('click', testProviderConnection);
        if (dom.saveProviderBtn) dom.saveProviderBtn.addEventListener('click', saveProviderConfig);

        // Trace Filters & Stream Controls
        if (dom.traceSearchInput) {
            dom.traceSearchInput.addEventListener('input', (e) => {
                traceSearchQuery = e.target.value.toLowerCase().trim();
                filterTraceStream();
            });
        }
        if (dom.traceFilterType) {
            dom.traceFilterType.addEventListener('change', (e) => {
                activeTraceFilter = e.target.value;
                filterTraceStream();
            });
        }
        if (dom.clearLogsBtn) dom.clearLogsBtn.addEventListener('click', clearTraceStream);
        if (dom.autoScrollToggle) dom.autoScrollToggle.addEventListener('change', (e) => autoScrollLogs = e.target.checked);
        if (dom.copyDiffBtn) dom.copyDiffBtn.addEventListener('click', copyActiveDiff);
    }

    // Submit Task
    async function handleTaskSubmit() {
        const prompt = dom.taskPromptInput ? dom.taskPromptInput.value.trim() : '';
        if (!prompt) {
            alert('Please enter a task description before running.');
            return;
        }
        await submitLiveAgentTask(prompt);
    }

    async function handleDemoTaskSubmit() {
        const demoPrompt = "Design & Build a Video Game CRUD API logic module";
        if (dom.taskPromptInput) dom.taskPromptInput.value = demoPrompt;
        await submitLiveAgentTask(demoPrompt);
    }

    async function submitLiveAgentTask(userRequest) {
        resetDashboardState();
        updateTaskStatusPill('RUNNING', 'Initializing Swarm');
        startTimer();

        try {
            const res = await fetch('/api/v1/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: 'proj-live-001',
                    user_request: userRequest
                })
            });

            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            currentTaskId = data.task_id;

            if (dom.metricTaskId) dom.metricTaskId.textContent = `#${data.task_id.slice(-8)}`;
            if (dom.metricTaskTitle) dom.metricTaskTitle.textContent = userRequest;

            appendTraceEntry({
                event_type: 'SYSTEM',
                message: `Task ${data.task_id} initialized in autonomous_agent_directory/${data.task_id}`,
                timestamp: formatTimestamp()
            });

            connectWebSocket(data.task_id);
        } catch (err) {
            updateTaskStatusPill('FAILED', 'Initialization Error');
            stopTimer();
            appendTraceEntry({
                event_type: 'ERROR',
                message: `Failed to launch task: ${err.message}`,
                timestamp: formatTimestamp()
            });
        }
    }

    // WebSocket Stream Connection
    function connectWebSocket(taskId) {
        if (wsSocket) wsSocket.close();

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/v1/tasks/${taskId}/stream`;

        updateWsStatus('connecting', 'Connecting...');
        wsSocket = new WebSocket(wsUrl);

        wsSocket.onopen = () => updateWsStatus('connected', 'Live Telemetry Active');

        wsSocket.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                handleStreamEvent(data);
            } catch (e) {
                console.error("Failed to parse WebSocket message:", e);
            }
        };

        wsSocket.onerror = () => updateWsStatus('disconnected', 'Connection Error');

        wsSocket.onclose = () => updateWsStatus('disconnected', 'Disconnected');
    }

    function updateWsStatus(state, text) {
        if (dom.wsStatusDot) {
            dom.wsStatusDot.className = `status-dot ${state}`;
        }
        if (dom.wsStatusText) dom.wsStatusText.textContent = text;
    }

    // Process WebSocket Event Stream
    function handleStreamEvent(data) {
        const eventType = data.event_type || (data.task ? 'SYSTEM' : 'INFO');
        const message = data.message || (data.task ? `Task status: ${data.task.status}` : 'Event received');
        const payload = data.payload || data.code_diff || null;

        appendTraceEntry({
            event_type: eventType,
            message: message,
            payload: payload,
            timestamp: formatTimestamp()
        });

        // Update Pipeline Stages & Node States
        if (data.message && data.message.includes('Architect Agent')) updateStageState('architect');
        else if (data.message && data.message.includes('Researcher Agent')) updateStageState('researcher');
        else if (data.message && data.message.includes('Coder Agent')) updateStageState('coder');
        else if (data.message && data.message.includes('Tester Agent')) updateStageState('tester');
        else if (data.message && data.message.includes('Sandbox Runner')) updateStageState('sandbox');
        else if (data.message && data.message.includes('Final Reviewer Agent')) updateStageState('reviewer');

        // Handle Code Diffs
        if (payload && payload.code_diff) {
            renderCodeDiff(payload.code_diff);
        } else if (data.code_diff) {
            renderCodeDiff(data.code_diff);
        }

        // Handle Final Completion or Failure
        if (eventType === 'SYSTEM' && message.includes('completed with status:')) {
            if (message.includes('COMPLETED')) {
                updateTaskStatusPill('COMPLETED', 'All Stages Passed');
                completeAllStages();
            } else {
                updateTaskStatusPill('FAILED', 'Check Debug Trace');
            }
            stopTimer();
        }
    }

    // Pipeline Stage Node Visualizer State Manager
    function updateStageState(activeStage) {
        const stageIndex = STAGES.indexOf(activeStage);
        if (stageIndex === -1) return;

        STAGES.forEach((stage, idx) => {
            const nodeEl = document.getElementById(`node-${stage}`);
            const arrowEl = document.getElementById(`arrow-${idx}-${idx + 1}`);

            if (!nodeEl) return;

            const badgeEl = nodeEl.querySelector('.node-status-badge');

            if (idx < stageIndex) {
                // Completed Stage
                nodeEl.className = 'dag-stage-node completed';
                if (badgeEl) { badgeEl.textContent = '✓ Done'; badgeEl.style.display = 'inline-block'; }
            } else if (idx === stageIndex) {
                // Active Stage
                nodeEl.className = 'dag-stage-node active';
                if (badgeEl) { badgeEl.textContent = 'Active'; badgeEl.style.display = 'inline-block'; }
                if (dom.activeAgentStageTag) dom.activeAgentStageTag.textContent = `${stage.toUpperCase()} Stage`;
            } else {
                // Pending Stage
                nodeEl.className = 'dag-stage-node pending';
                if (badgeEl) badgeEl.style.display = 'none';
            }

            if (arrowEl) {
                if (idx < stageIndex) arrowEl.className = 'pipeline-arrow active';
                else arrowEl.className = 'pipeline-arrow';
            }
        });

        // Update Progress Bar
        const completedCount = stageIndex + 1;
        const percent = Math.min(Math.round((completedCount / STAGES.length) * 100), 100);
        if (dom.metricProgressText) dom.metricProgressText.textContent = `${completedCount} / 6 Stage`;
        if (dom.metricProgressPercent) dom.metricProgressPercent.textContent = `${percent}%`;
        if (dom.metricProgressBar) dom.metricProgressBar.style.width = `${percent}%`;
    }

    function completeAllStages() {
        STAGES.forEach((stage, idx) => {
            const nodeEl = document.getElementById(`node-${stage}`);
            const arrowEl = document.getElementById(`arrow-${idx}-${idx + 1}`);
            if (nodeEl) {
                nodeEl.className = 'dag-stage-node completed';
                const badgeEl = nodeEl.querySelector('.node-status-badge');
                if (badgeEl) { badgeEl.textContent = '✓ Done'; badgeEl.style.display = 'inline-block'; }
            }
            if (arrowEl) arrowEl.className = 'pipeline-arrow active';
        });

        if (dom.metricProgressText) dom.metricProgressText.textContent = `6 / 6 Stage`;
        if (dom.metricProgressPercent) dom.metricProgressPercent.textContent = `100%`;
        if (dom.metricProgressBar) dom.metricProgressBar.style.width = `100%`;
        if (dom.activeAgentStageTag) dom.activeAgentStageTag.textContent = `Workflow Completed`;
    }

    // Code Diff Inspector
    function renderCodeDiff(codeDiff) {
        if (!codeDiff || !codeDiff.filename || !codeDiff.lines) return;

        const filename = codeDiff.filename;
        let fileContent = '';
        let addCount = 0;
        let delCount = 0;

        codeDiff.lines.forEach(line => {
            const text = typeof line === 'string' ? line : line.text;
            const type = line.type || 'add';
            if (type === 'add') {
                fileContent += `+ ${text}\n`;
                addCount++;
            } else if (type === 'del') {
                fileContent += `- ${text}\n`;
                delCount++;
            } else {
                fileContent += `  ${text}\n`;
            }
        });

        activeDiffFiles[filename] = { content: fileContent, addCount, delCount, lines: codeDiff.lines };
        renderDiffTabs();
        switchDiffTab(filename);

        // Update Code Mod Metrics
        let totalAdds = 0;
        let totalDels = 0;
        Object.values(activeDiffFiles).forEach(item => {
            totalAdds += item.addCount;
            totalDels += item.delCount;
        });

        if (dom.metricAdditions) dom.metricAdditions.textContent = `+${totalAdds}`;
        if (dom.metricDeletions) dom.metricDeletions.textContent = `-${totalDels}`;
        if (dom.metricFilesChanged) dom.metricFilesChanged.textContent = `${Object.keys(activeDiffFiles).length} Files Modified`;
    }

    function renderDiffTabs() {
        if (!dom.diffTabsHeader) return;
        dom.diffTabsHeader.innerHTML = '';

        Object.keys(activeDiffFiles).forEach(filename => {
            const tab = document.createElement('div');
            tab.className = `diff-tab ${filename === currentActiveDiffFile ? 'active' : ''}`;
            tab.setAttribute('data-filename', filename);
            tab.innerHTML = `
                <span class="tab-dot"></span>
                <span class="tab-name">${filename}</span>
            `;
            tab.addEventListener('click', () => switchDiffTab(filename));
            dom.diffTabsHeader.appendChild(tab);
        });
    }

    function switchDiffTab(filename) {
        currentActiveDiffFile = filename;
        renderDiffTabs();

        if (dom.diffFilePath) dom.diffFilePath.textContent = `autonomous_agent_directory/${filename}`;

        const fileData = activeDiffFiles[filename];
        if (!fileData) return;

        if (dom.diffPlaceholder) dom.diffPlaceholder.classList.add('hidden');
        if (dom.diffCodeWrapper) dom.diffCodeWrapper.classList.remove('hidden');

        if (dom.diffContentPre) {
            dom.diffContentPre.innerHTML = '';
            fileData.lines.forEach(line => {
                const text = typeof line === 'string' ? line : line.text;
                const type = line.type || 'add';
                const lineSpan = document.createElement('span');

                if (type === 'add') lineSpan.className = 'diff-line-add';
                else if (type === 'del') lineSpan.className = 'diff-line-del';
                else lineSpan.className = 'diff-line-info';

                lineSpan.textContent = (type === 'add' ? '+ ' : type === 'del' ? '- ' : '  ') + text;
                dom.diffContentPre.appendChild(lineSpan);
            });
        }
    }

    function copyActiveDiff() {
        if (!currentActiveDiffFile || !activeDiffFiles[currentActiveDiffFile]) {
            alert('No active code diff to copy.');
            return;
        }
        navigator.clipboard.writeText(activeDiffFiles[currentActiveDiffFile].content);
        if (dom.copyDiffBtn) {
            const orig = dom.copyDiffBtn.innerHTML;
            dom.copyDiffBtn.innerHTML = '✓ Copied!';
            setTimeout(() => dom.copyDiffBtn.innerHTML = orig, 1500);
        }
    }

    // Event Trace Stream Logging
    function appendTraceEntry(entry) {
        if (!dom.traceEntriesList) return;

        const item = document.createElement('div');
        item.className = 'trace-item';
        item.setAttribute('data-type', entry.event_type);

        const typeTagClass = `tag-${(entry.event_type || 'system').toLowerCase()}`;

        let payloadHtml = '';
        if (entry.payload && Object.keys(entry.payload).length > 0) {
            const formatted = JSON.stringify(entry.payload, null, 2);
            payloadHtml = `<pre class="trace-payload">${escapeHtml(formatted)}</pre>`;
        }

        item.innerHTML = `
            <div class="trace-item-header">
                <div class="trace-meta">
                    <span class="event-tag ${typeTagClass}">${entry.event_type}</span>
                    <span class="trace-time">${entry.timestamp}</span>
                </div>
            </div>
            <div class="trace-message">${escapeHtml(entry.message)}</div>
            ${payloadHtml}
        `;

        dom.traceEntriesList.appendChild(item);
        filterTraceStream();

        // Increment Token Usage Estimate
        promptTokens += Math.round(entry.message.length / 4);
        completionTokens += entry.payload ? Math.round(JSON.stringify(entry.payload).length / 4) : 0;
        totalTokens = promptTokens + completionTokens;

        if (dom.metricTokens) dom.metricTokens.innerHTML = `${totalTokens.toLocaleString()} <span class="unit">tk</span>`;
        if (dom.metricTokensSub) dom.metricTokensSub.textContent = `Prompt: ${promptTokens.toLocaleString()} | Completion: ${completionTokens.toLocaleString()}`;

        if (autoScrollLogs && dom.traceStreamContainer) {
            dom.traceStreamContainer.scrollTop = dom.traceStreamContainer.scrollHeight;
        }
    }

    function filterTraceStream() {
        if (!dom.traceEntriesList) return;
        const items = dom.traceEntriesList.querySelectorAll('.trace-item');

        items.forEach(item => {
            const itemType = item.getAttribute('data-type') || 'ALL';
            const text = item.textContent.toLowerCase();

            const matchesType = (activeTraceFilter === 'ALL' || itemType === activeTraceFilter);
            const matchesQuery = (!traceSearchQuery || text.includes(traceSearchQuery));

            if (matchesType && matchesQuery) {
                item.classList.remove('hidden');
            } else {
                item.classList.add('hidden');
            }
        });
    }

    function clearTraceStream() {
        if (dom.traceEntriesList) dom.traceEntriesList.innerHTML = '';
    }

    // Model Provider Modal Controls
    function showModal(show) {
        if (!dom.providerModal) return;
        if (show) dom.providerModal.classList.remove('hidden');
        else dom.providerModal.classList.add('hidden');
    }

    function handleProviderSelectChange(e) {
        const val = e.target.value;
        if (val === 'ollama') {
            dom.baseUrlInput.value = 'http://localhost:11434/v1';
            dom.baseUrlHint.textContent = 'Ollama local server endpoint (http://localhost:11434/v1). API Key is not required.';
        } else if (val === 'gemini') {
            dom.baseUrlInput.value = '';
            dom.baseUrlHint.textContent = 'Google Gemini Cloud Provider. Uses environment API key by default.';
        } else if (val === 'openai') {
            dom.baseUrlInput.value = 'https://api.openai.com/v1';
            dom.baseUrlHint.textContent = 'OpenAI Cloud Provider (GPT-4o).';
        }
    }

    async function loadActiveProviderConfig() {
        try {
            const res = await fetch('/api/v1/provider-config');
            if (res.ok) {
                const config = await res.json();
                if (dom.providerSelect) dom.providerSelect.value = config.provider || 'ollama';
                if (dom.baseUrlInput) dom.baseUrlInput.value = config.base_url || '';
                if (dom.modelNameInput) dom.modelNameInput.value = config.model_name || 'qwen2.5-coder';
                if (dom.tempInput) {
                    dom.tempInput.value = config.temperature || 0.2;
                    dom.tempValue.textContent = config.temperature || 0.2;
                }
                if (dom.activeModelText) dom.activeModelText.textContent = `${config.provider.toUpperCase()}: ${config.model_name}`;
            }
        } catch (e) {
            console.error("Failed to load provider config:", e);
        }
    }

    async function testProviderConnection() {
        if (!dom.connectionStatusBox) return;
        dom.connectionStatusBox.className = 'connection-status-box info';
        dom.connectionStatusBox.textContent = 'Testing connectivity to LLM provider server...';
        dom.connectionStatusBox.classList.remove('hidden');

        const config = {
            provider: dom.providerSelect.value,
            model_name: dom.modelNameInput.value.trim() || 'qwen2.5-coder',
            base_url: dom.baseUrlInput.value.trim(),
            api_key: dom.apiKeyInput.value.trim(),
            temperature: parseFloat(dom.tempInput.value)
        };

        try {
            const res = await fetch('/api/v1/provider-config/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
            const data = await res.json();

            if (data.success) {
                dom.connectionStatusBox.className = 'connection-status-box success';
                dom.connectionStatusBox.textContent = `✓ ${data.message}`;

                if (data.available_models && data.available_models.length > 0) {
                    populateModelDropdown(data.available_models);
                }
            } else {
                dom.connectionStatusBox.className = 'connection-status-box error';
                dom.connectionStatusBox.textContent = `✕ ${data.message}`;
            }
        } catch (err) {
            dom.connectionStatusBox.className = 'connection-status-box error';
            dom.connectionStatusBox.textContent = `✕ Server error: ${err.message}`;
        }
    }

    async function fetchInstalledModels() {
        await testProviderConnection();
    }

    function populateModelDropdown(models) {
        if (!dom.modelDropdownSelect) return;
        dom.modelDropdownSelect.innerHTML = '<option value="">-- Select Installed Model --</option>';
        models.forEach(model => {
            const opt = document.createElement('option');
            opt.value = model;
            opt.textContent = model;
            dom.modelDropdownSelect.appendChild(opt);
        });
    }

    async function saveProviderConfig() {
        const config = {
            provider: dom.providerSelect.value,
            model_name: dom.modelNameInput.value.trim() || 'qwen2.5-coder',
            base_url: dom.baseUrlInput.value.trim(),
            api_key: dom.apiKeyInput.value.trim(),
            temperature: parseFloat(dom.tempInput.value)
        };

        try {
            const res = await fetch('/api/v1/provider-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
            if (res.ok) {
                if (dom.activeModelText) dom.activeModelText.textContent = `${config.provider.toUpperCase()}: ${config.model_name}`;
                showModal(false);
                alert(`Provider configuration updated to ${config.provider} (${config.model_name})!`);
            }
        } catch (e) {
            alert(`Failed to save provider config: ${e.message}`);
        }
    }

    // Live Execution Timer
    function startTimer() {
        stopTimer();
        startTime = Date.now();
        timerInterval = setInterval(() => {
            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            const hrs = String(Math.floor(elapsed / 3600)).padStart(2, '0');
            const mins = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
            const secs = String(elapsed % 60).padStart(2, '0');
            if (dom.metricRuntime) dom.metricRuntime.textContent = `${hrs}:${mins}:${secs}`;
        }, 1000);
    }

    function stopTimer() {
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = null;
    }

    // Helper Utilities
    function resetDashboardState() {
        stopTimer();
        currentTaskId = null;
        activeDiffFiles = {};
        currentActiveDiffFile = null;
        totalTokens = 0;
        promptTokens = 0;
        completionTokens = 0;

        if (dom.metricStatusPill) dom.metricStatusPill.className = 'status-pill status-pending';
        if (dom.metricStatusPill) dom.metricStatusPill.textContent = 'IDLE';
        if (dom.metricTaskId) dom.metricTaskId.textContent = '#ready';
        if (dom.metricRuntime) dom.metricRuntime.textContent = '00:00:00';
        if (dom.metricTokens) dom.metricTokens.innerHTML = '0 <span class="unit">tk</span>';
        if (dom.metricAdditions) dom.metricAdditions.textContent = '+0';
        if (dom.metricDeletions) dom.metricDeletions.textContent = '-0';
        if (dom.metricFilesChanged) dom.metricFilesChanged.textContent = '0 Workspace Files Modified';
        if (dom.metricProgressText) dom.metricProgressText.textContent = '0 / 6 Stage';
        if (dom.metricProgressPercent) dom.metricProgressPercent.textContent = '0%';
        if (dom.metricProgressBar) dom.metricProgressBar.style.width = '0%';

        // Reset Pipeline Stage Nodes
        STAGES.forEach((stage, idx) => {
            const nodeEl = document.getElementById(`node-${stage}`);
            const arrowEl = document.getElementById(`arrow-${idx}-${idx + 1}`);
            if (nodeEl) {
                nodeEl.className = 'dag-stage-node pending';
                const badgeEl = nodeEl.querySelector('.node-status-badge');
                if (badgeEl) badgeEl.style.display = 'none';
            }
            if (arrowEl) arrowEl.className = 'pipeline-arrow';
        });

        if (dom.diffPlaceholder) dom.diffPlaceholder.classList.remove('hidden');
        if (dom.diffCodeWrapper) dom.diffCodeWrapper.classList.add('hidden');
        if (dom.diffTabsHeader) dom.diffTabsHeader.innerHTML = '';
        if (dom.diffFilePath) dom.diffFilePath.textContent = 'autonomous_agent_directory/';

        clearTraceStream();
    }

    function updateTaskStatusPill(status, text) {
        if (!dom.metricStatusPill) return;
        const cls = status.toLowerCase();
        dom.metricStatusPill.className = `status-pill status-${cls}`;
        dom.metricStatusPill.textContent = text || status;
    }

    function formatTimestamp() {
        const d = new Date();
        return d.toTimeString().split(' ')[0];
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // Run Initialization
    document.addEventListener('DOMContentLoaded', init);

})();
