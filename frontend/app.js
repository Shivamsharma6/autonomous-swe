/**
 * Autonomous SWE Platform - Realtime Dashboard Client
 * WebSocket Client for /api/v1/tasks/{id}/stream
 */

document.addEventListener('DOMContentLoaded', () => {
    // --- Application State ---
    const state = {
        taskId: 'task-1001',
        ws: null,
        isConnected: false,
        isDemoMode: false,
        demoInterval: null,
        startTime: null,
        timerInterval: null,
        metrics: {
            status: 'PENDING',
            runtimeSeconds: 0,
            tokensPrompt: 0,
            tokensCompletion: 0,
            additions: 0,
            deletions: 0,
            filesChanged: 0,
            completedSteps: 0,
            totalSteps: 5
        },
        dagNodes: [
            { id: 'node-1', title: 'Context & Requirements Analysis', status: 'PENDING' },
            { id: 'node-2', title: 'Task DAG & Plan Generation', status: 'PENDING' },
            { id: 'node-3', title: 'Code Search & Symbol Resolution', status: 'PENDING' },
            { id: 'node-4', title: 'Code Refactoring & Implementation', status: 'PENDING' },
            { id: 'node-5', title: 'Verification & Test Suite', status: 'PENDING' }
        ],
        codeDiffs: {
            'autoswe/control_plane.py': [
                { type: 'info', text: '@@ -112,6 +112,18 @@ async def websocket_stream' },
                { type: 'same', text: ' @app.websocket("/api/v1/tasks/{task_id}/stream")' },
                { type: 'same', text: ' async def websocket_stream(websocket: WebSocket, task_id: str) -> None:' },
                { type: 'del', text: '-    await websocket.send_json({"task_id": task_id, "status": "connected"})' },
                { type: 'add', text: '+    await manager.connect(websocket)' },
                { type: 'add', text: '+    try:' },
                { type: 'add', text: '+        task = await asyncio.to_thread(storage.get_task, task_id)' },
                { type: 'add', text: '+        await websocket.send_json({"task_id": task_id, "task": task, "timestamp": time.time()})' },
                { type: 'add', text: '+        while True:' },
                { type: 'add', text: '+            data = await websocket.receive_text()' },
                { type: 'add', text: '+            await websocket.send_json({"task_id": task_id, "data": data})' },
                { type: 'add', text: '+    except WebSocketDisconnect:' },
                { type: 'add', text: '+        manager.disconnect(websocket)' }
            ],
            'frontend/app.js': [
                { type: 'info', text: '@@ -1,5 +1,12 @@' },
                { type: 'add', text: '+// Initialize WebSocket connection to Autonomous SWE backend' },
                { type: 'add', text: '+const wsUrl = `ws://${window.location.host}/api/v1/tasks/${taskId}/stream`;' },
                { type: 'same', text: ' function connectWebSocket(taskId) {' },
                { type: 'del', text: '-    console.log("Connecting...");' },
                { type: 'add', text: '+    state.ws = new WebSocket(wsUrl);' }
            ]
        },
        activeDiffFile: 'autoswe/control_plane.py',
        traceEvents: [],
        autoScroll: true,
        filterType: 'ALL',
        searchQuery: ''
    };

    // --- DOM Elements ---
    const dom = {
        taskIdInput: document.getElementById('taskIdInput'),
        connectBtn: document.getElementById('connectBtn'),
        demoToggleBtn: document.getElementById('demoToggleBtn'),
        wsStatusBadge: document.getElementById('wsStatusBadge'),
        wsStatusText: document.getElementById('wsStatusText'),
        
        // Metrics
        metricStatusPill: document.getElementById('metricStatusPill'),
        metricTaskTitle: document.getElementById('metricTaskTitle'),
        metricRuntime: document.getElementById('metricRuntime'),
        metricTokens: document.getElementById('metricTokens'),
        metricTokensSub: document.getElementById('metricTokensSub'),
        metricAdditions: document.getElementById('metricAdditions'),
        metricDeletions: document.getElementById('metricDeletions'),
        metricFilesChanged: document.getElementById('metricFilesChanged'),
        metricProgressText: document.getElementById('metricProgressText'),
        metricProgressBar: document.getElementById('metricProgressBar'),

        // Panels
        dagGraphView: document.getElementById('dagGraphView'),
        resetDagBtn: document.getElementById('resetDagBtn'),
        diffFilePath: document.getElementById('diffFilePath'),
        diffTabsHeader: document.getElementById('diffTabsHeader'),
        diffPlaceholder: document.getElementById('diffPlaceholder'),
        diffContentPre: document.getElementById('diffContentPre'),
        copyDiffBtn: document.getElementById('copyDiffBtn'),

        // Trace Stream
        traceStreamContainer: document.getElementById('traceStreamContainer'),
        traceEntriesList: document.getElementById('traceEntriesList'),
        clearLogsBtn: document.getElementById('clearLogsBtn'),
        autoScrollToggle: document.getElementById('autoScrollToggle'),
        traceSearchInput: document.getElementById('traceSearchInput'),
        traceFilterType: document.getElementById('traceFilterType'),

        // Model Provider Config Modal
        providerConfigBtn: document.getElementById('providerConfigBtn'),
        providerModal: document.getElementById('providerModal'),
        closeModalBtn: document.getElementById('closeModalBtn'),
        providerSelect: document.getElementById('providerSelect'),
        baseUrlInput: document.getElementById('baseUrlInput'),
        apiKeyInput: document.getElementById('apiKeyInput'),
        modelNameInput: document.getElementById('modelNameInput'),
        tempInput: document.getElementById('tempInput'),
        tempValue: document.getElementById('tempValue'),
        testProviderBtn: document.getElementById('testProviderBtn'),
        saveProviderBtn: document.getElementById('saveProviderBtn'),
        connectionStatusBox: document.getElementById('connectionStatusBox')
    };


    // --- Core Initialization ---
    init();

    function init() {
        bindEvents();
        renderDAG();
        renderDiffTabs();
        renderDiffContent();
        loadProviderConfig();
        
        // Auto connect or set initial status
        setWSStatus('disconnected', 'Disconnected');
    }


    function bindEvents() {
        dom.connectBtn.addEventListener('click', () => {
            const taskId = dom.taskIdInput.value.trim() || 'task-1001';
            connectWebSocket(taskId);
        });

        dom.demoToggleBtn.addEventListener('click', () => {
            if (state.isDemoMode) {
                stopDemoStream();
            } else {
                startDemoStream();
            }
        });

        dom.resetDagBtn.addEventListener('click', () => {
            renderDAG();
        });

        dom.copyDiffBtn.addEventListener('click', () => {
            copyCurrentDiff();
        });

        dom.clearLogsBtn.addEventListener('click', () => {
            state.traceEvents = [];
            dom.traceEntriesList.innerHTML = '';
        });

        dom.autoScrollToggle.addEventListener('change', (e) => {
            state.autoScroll = e.target.checked;
        });

        dom.traceSearchInput.addEventListener('input', (e) => {
            state.searchQuery = e.target.value.toLowerCase();
            filterTraceEntries();
        });

        dom.traceFilterType.addEventListener('change', (e) => {
            state.filterType = e.target.value;
            filterTraceEntries();
        });

        // Provider Modal Events
        dom.providerConfigBtn.addEventListener('click', () => {
            dom.providerModal.classList.remove('hidden');
        });

        dom.closeModalBtn.addEventListener('click', () => {
            dom.providerModal.classList.add('hidden');
        });

        dom.providerSelect.addEventListener('change', (e) => {
            const provider = e.target.value;
            if (provider === 'ollama') {
                dom.baseUrlInput.value = 'http://localhost:11434/v1';
                dom.modelNameInput.value = 'qwen2.5-coder';
                dom.apiKeyInput.value = '';
            } else if (provider === 'unsloth') {
                dom.baseUrlInput.value = 'http://localhost:8080/v1';
                dom.modelNameInput.value = 'unsloth-deepseek-r1';
                dom.apiKeyInput.value = '';
            } else if (provider === 'custom') {
                if (!dom.baseUrlInput.value) {
                    dom.baseUrlInput.value = 'http://localhost:8080/v1';
                }
                dom.apiKeyInput.value = '';
            } else {
                dom.baseUrlInput.value = '';
                if (provider === 'gemini') dom.modelNameInput.value = 'gemini-3.6-flash';
                if (provider === 'openai') dom.modelNameInput.value = 'gpt-4o';
                if (provider === 'anthropic') dom.modelNameInput.value = 'claude-3-5-sonnet';
            }
        });

        dom.tempInput.addEventListener('input', (e) => {
            dom.tempValue.textContent = e.target.value;
        });

        dom.testProviderBtn.addEventListener('click', () => {
            testProviderConnection();
        });

        dom.saveProviderBtn.addEventListener('click', () => {
            saveProviderConfig();
        });
    }



    // --- WebSocket Stream Client ---
    function connectWebSocket(taskId) {
        stopDemoStream();
        if (state.ws) {
            state.ws.close();
        }

        state.taskId = taskId;
        dom.metricTaskTitle.textContent = `Task #${taskId}`;
        setWSStatus('connecting', 'Connecting...');

        // Protocol & Host determination
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        let host = window.location.host;
        // If served statically without port or file://, fallback to default API server port 8000
        if (!host || window.location.protocol === 'file:') {
            host = '127.0.0.1:8000';
        }
        
        const wsUrl = `${protocol}//${host}/api/v1/tasks/${taskId}/stream`;
        appendTraceLog('SYSTEM', `Initiating WebSocket stream connection to ${wsUrl}`);

        try {
            state.ws = new WebSocket(wsUrl);

            state.ws.onopen = () => {
                state.isConnected = true;
                setWSStatus('connected', 'Connected');
                appendTraceLog('SYSTEM', `Stream connected successfully for task ${taskId}`);
                startRuntimeTimer();
            };

            state.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    handleStreamPayload(data);
                } catch (e) {
                    appendTraceLog('SYSTEM', `Raw message received: ${event.data}`);
                }
            };

            state.ws.onerror = (err) => {
                appendTraceLog('ERROR', `WebSocket error encountered.`);
            };

            state.ws.onclose = () => {
                state.isConnected = false;
                setWSStatus('disconnected', 'Disconnected');
                appendTraceLog('SYSTEM', 'WebSocket connection closed.');
                stopRuntimeTimer();
            };
        } catch (e) {
            setWSStatus('disconnected', 'Failed to connect');
            appendTraceLog('ERROR', `Connection failed: ${e.message}`);
        }
    }

    function setWSStatus(statusClass, text) {
        dom.wsStatusBadge.querySelector('.status-dot').className = `status-dot ${statusClass}`;
        dom.wsStatusText.textContent = text;
    }

    // --- Incoming Stream Payload Handler ---
    function handleStreamPayload(data) {
        // If task detail object present
        if (data.task) {
            state.metrics.status = data.task.status || state.metrics.status;
            updateMetricsUI();
        }

        // If generic text payload or custom event
        if (data.data) {
            appendTraceLog('TOOL', `Stream payload: ${typeof data.data === 'object' ? JSON.stringify(data.data) : data.data}`);
        }

        // If structured event payload
        if (data.event_type) {
            appendTraceLog(data.event_type, data.message || '', data.payload);
        }

        // If code diff update
        if (data.code_diff) {
            updateCodeDiff(data.code_diff.filename, data.code_diff.lines);
        }

        // If DAG update
        if (data.dag_nodes) {
            state.dagNodes = data.dag_nodes;
            renderDAG();
        }
    }

    // --- Metrics & Timer ---
    function startRuntimeTimer() {
        if (state.timerInterval) clearInterval(state.timerInterval);
        state.startTime = Date.now() - (state.metrics.runtimeSeconds * 1000);
        state.timerInterval = setInterval(() => {
            state.metrics.runtimeSeconds = Math.floor((Date.now() - state.startTime) / 1000);
            updateTimerUI();
        }, 1000);
    }

    function stopRuntimeTimer() {
        if (state.timerInterval) {
            clearInterval(state.timerInterval);
            state.timerInterval = null;
        }
    }

    function updateTimerUI() {
        const hrs = String(Math.floor(state.metrics.runtimeSeconds / 3600)).padStart(2, '0');
        const mins = String(Math.floor((state.metrics.runtimeSeconds % 3600) / 60)).padStart(2, '0');
        const secs = String(state.metrics.runtimeSeconds % 60).padStart(2, '0');
        dom.metricRuntime.textContent = `${hrs}:${mins}:${secs}`;
    }

    function updateMetricsUI() {
        // Status Pill
        const status = state.metrics.status.toUpperCase();
        dom.metricStatusPill.textContent = status;
        dom.metricStatusPill.className = `status-pill status-${status.toLowerCase()}`;

        // Tokens
        const totalTokens = state.metrics.tokensPrompt + state.metrics.tokensCompletion;
        dom.metricTokens.textContent = totalTokens.toLocaleString();
        dom.metricTokensSub.textContent = `Prompt: ${state.metrics.tokensPrompt.toLocaleString()} | Completion: ${state.metrics.tokensCompletion.toLocaleString()}`;

        // Diffs
        dom.metricAdditions.textContent = `+${state.metrics.additions}`;
        dom.metricDeletions.textContent = `-${state.metrics.deletions}`;
        dom.metricFilesChanged.textContent = `${state.metrics.filesChanged} File${state.metrics.filesChanged === 1 ? '' : 's'} Changed`;

        // Progress
        dom.metricProgressText.textContent = `${state.metrics.completedSteps} / ${state.metrics.totalSteps}`;
        const pct = Math.min(100, Math.round((state.metrics.completedSteps / state.metrics.totalSteps) * 100));
        dom.metricProgressBar.style.width = `${pct}%`;
    }

    // --- DAG Graph Renderer ---
    function renderDAG() {
        dom.dagGraphView.innerHTML = '';
        state.dagNodes.forEach((node, index) => {
            const nodeEl = document.createElement('div');
            nodeEl.className = `dag-node ${node.status.toLowerCase()}`;
            
            let statusColor = '#94a3b8';
            if (node.status === 'RUNNING') statusColor = 'var(--primary-cyan)';
            if (node.status === 'COMPLETED') statusColor = 'var(--success-emerald)';
            if (node.status === 'FAILED') statusColor = 'var(--danger-rose)';

            nodeEl.innerHTML = `
                <div class="dag-node-header">
                    <span class="dag-node-id">${node.id}</span>
                    <span class="dag-node-status" style="background-color: ${statusColor}; shadow: 0 0 8px ${statusColor}"></span>
                </div>
                <div class="dag-node-title">${node.title}</div>
            `;
            dom.dagGraphView.appendChild(nodeEl);

            // Add connector line if not last node
            if (index < state.dagNodes.length - 1) {
                const connector = document.createElement('div');
                connector.className = `dag-connector ${node.status === 'COMPLETED' ? 'active' : ''}`;
                dom.dagGraphView.appendChild(connector);
            }
        });
    }

    // --- Code Diff Viewer Renderer ---
    function renderDiffTabs() {
        dom.diffTabsHeader.innerHTML = '';
        const filenames = Object.keys(state.codeDiffs);

        if (filenames.length === 0) {
            dom.diffFilePath.textContent = 'No file selected';
            return;
        }

        filenames.forEach(filename => {
            const tab = document.createElement('div');
            tab.className = `diff-tab ${filename === state.activeDiffFile ? 'active' : ''}`;
            tab.textContent = filename.split('/').pop();
            tab.title = filename;
            tab.addEventListener('click', () => {
                state.activeDiffFile = filename;
                renderDiffTabs();
                renderDiffContent();
            });
            dom.diffTabsHeader.appendChild(tab);
        });

        if (!filenames.includes(state.activeDiffFile)) {
            state.activeDiffFile = filenames[0];
        }
        dom.diffFilePath.textContent = state.activeDiffFile;
    }

    function renderDiffContent() {
        const lines = state.codeDiffs[state.activeDiffFile];
        if (!lines || lines.length === 0) {
            dom.diffPlaceholder.classList.remove('hidden');
            dom.diffContentPre.classList.add('hidden');
            return;
        }

        dom.diffPlaceholder.classList.add('hidden');
        dom.diffContentPre.classList.remove('hidden');

        dom.diffContentPre.innerHTML = lines.map(line => {
            let className = '';
            if (line.type === 'add') className = 'diff-line-add';
            else if (line.type === 'del') className = 'diff-line-del';
            else if (line.type === 'info') className = 'diff-line-info';
            
            const textEscaped = escapeHtml(line.text);
            return `<span class="${className}">${textEscaped}</span>`;
        }).join('\n');
    }

    function updateCodeDiff(filename, lines) {
        state.codeDiffs[filename] = lines;
        state.metrics.filesChanged = Object.keys(state.codeDiffs).length;
        
        let additions = 0;
        let deletions = 0;
        Object.values(state.codeDiffs).forEach(fileLines => {
            fileLines.forEach(l => {
                if (l.type === 'add') additions++;
                if (l.type === 'del') deletions++;
            });
        });
        state.metrics.additions = additions;
        state.metrics.deletions = deletions;

        updateMetricsUI();
        renderDiffTabs();
        renderDiffContent();
    }

    function copyCurrentDiff() {
        const lines = state.codeDiffs[state.activeDiffFile];
        if (lines) {
            const rawText = lines.map(l => l.text).join('\n');
            navigator.clipboard.writeText(rawText).then(() => {
                appendTraceLog('SYSTEM', `Copied diff for ${state.activeDiffFile} to clipboard.`);
            });
        }
    }

    // --- Trace Stream Log Handler ---
    function appendTraceLog(eventType, message, payload = null) {
        const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
        const entry = { id: Date.now() + Math.random(), timestamp, eventType, message, payload };
        state.traceEvents.push(entry);

        renderTraceEntry(entry);
    }

    function renderTraceEntry(entry) {
        if (!shouldDisplayTrace(entry)) return;

        const item = document.createElement('div');
        item.className = 'trace-item';

        const tagClass = `tag-${(entry.eventType || 'system').toLowerCase()}`;
        
        let payloadHtml = '';
        if (entry.payload) {
            const payloadStr = typeof entry.payload === 'object' ? JSON.stringify(entry.payload, null, 2) : String(entry.payload);
            payloadHtml = `<pre class="trace-payload">${escapeHtml(payloadStr)}</pre>`;
        }

        item.innerHTML = `
            <div class="trace-item-header">
                <div class="trace-meta">
                    <span class="trace-time">${entry.timestamp}</span>
                    <span class="event-tag ${tagClass}">${entry.eventType}</span>
                </div>
            </div>
            <div class="trace-message">${escapeHtml(entry.message)}</div>
            ${payloadHtml}
        `;

        dom.traceEntriesList.appendChild(item);

        if (state.autoScroll) {
            dom.traceStreamContainer.scrollTop = dom.traceStreamContainer.scrollHeight;
        }
    }

    function filterTraceEntries() {
        dom.traceEntriesList.innerHTML = '';
        state.traceEvents.forEach(entry => {
            if (shouldDisplayTrace(entry)) {
                renderTraceEntry(entry);
            }
        });
    }

    function shouldDisplayTrace(entry) {
        if (state.filterType !== 'ALL' && entry.eventType.toUpperCase() !== state.filterType) {
            return false;
        }
        if (state.searchQuery) {
            const matchMsg = entry.message.toLowerCase().includes(state.searchQuery);
            const matchPayload = entry.payload && JSON.stringify(entry.payload).toLowerCase().includes(state.searchQuery);
            if (!matchMsg && !matchPayload) return false;
        }
        return true;
    }

    // --- Synthetic Demo Event Streamer ---
    function startDemoStream() {
        stopDemoStream();
        state.isDemoMode = true;
        dom.demoToggleBtn.classList.add('btn-primary');
        dom.demoToggleBtn.classList.remove('btn-secondary');
        dom.demoToggleBtn.textContent = 'Stop Demo';
        
        setWSStatus('connected', 'Demo Streaming');
        state.metrics.status = 'RUNNING';
        state.metrics.completedSteps = 0;
        state.metrics.tokensPrompt = 1250;
        state.metrics.tokensCompletion = 340;
        state.metrics.runtimeSeconds = 0;
        updateMetricsUI();
        startRuntimeTimer();

        appendTraceLog('SYSTEM', 'Started synthetic event stream demo mode');

        const demoSequence = [
            () => {
                state.dagNodes[0].status = 'RUNNING';
                renderDAG();
                appendTraceLog('THOUGHT', 'Analyzing user requirements: Single-page dashboard with glassmorphism UI & live WebSocket stream client.');
            },
            () => {
                state.metrics.tokensPrompt += 850;
                state.metrics.tokensCompletion += 420;
                state.dagNodes[0].status = 'COMPLETED';
                state.dagNodes[1].status = 'RUNNING';
                state.metrics.completedSteps = 1;
                updateMetricsUI();
                renderDAG();
                appendTraceLog('TOOL', 'Executing ripgrep search in workspace to locate existing API models & endpoints.', { query: '/api/v1/tasks/{id}/stream', path: 'autoswe/' });
            },
            () => {
                state.metrics.tokensPrompt += 1400;
                state.metrics.tokensCompletion += 610;
                state.dagNodes[1].status = 'COMPLETED';
                state.dagNodes[2].status = 'RUNNING';
                state.metrics.completedSteps = 2;
                updateMetricsUI();
                renderDAG();
                appendTraceLog('THOUGHT', 'Inspected control_plane.py. WebSocket route /api/v1/tasks/{task_id}/stream discovered.');
            },
            () => {
                state.dagNodes[2].status = 'COMPLETED';
                state.dagNodes[3].status = 'RUNNING';
                state.metrics.completedSteps = 3;
                updateMetricsUI();
                renderDAG();
                
                updateCodeDiff('frontend/index.html', [
                    { type: 'info', text: '@@ -1,10 +1,15 @@' },
                    { type: 'add', text: '+<!DOCTYPE html>' },
                    { type: 'add', text: '+<html lang="en">' },
                    { type: 'add', text: '+<head>' },
                    { type: 'add', text: '+    <title>Autonomous SWE Platform</title>' },
                    { type: 'add', text: '+    <link rel="stylesheet" href="styles.css">' },
                    { type: 'add', text: '+</head>' }
                ]);
                appendTraceLog('CODE', 'Created frontend/index.html dashboard structure with Header, Metrics, DAG & Event Stream.');
            },
            () => {
                state.metrics.tokensPrompt += 2100;
                state.metrics.tokensCompletion += 950;
                updateMetricsUI();
                appendTraceLog('TOOL', 'Writing frontend/styles.css with dark glassmorphism theme tokens and grid layout.');
                updateCodeDiff('frontend/styles.css', [
                    { type: 'info', text: '@@ -0,0 +1,18 @@' },
                    { type: 'add', text: '+:root {' },
                    { type: 'add', text: '+    --bg-main: #070a11;' },
                    { type: 'add', text: '+    --glass-bg: rgba(15, 21, 33, 0.65);' },
                    { type: 'add', text: '+    --primary-cyan: #00f2fe;' },
                    { type: 'add', text: '+}' },
                    { type: 'add', text: '+.glass-card { backdrop-filter: blur(16px); }' }
                ]);
            },
            () => {
                state.dagNodes[3].status = 'COMPLETED';
                state.dagNodes[4].status = 'RUNNING';
                state.metrics.completedSteps = 4;
                updateMetricsUI();
                renderDAG();
                appendTraceLog('TEST', 'Executing automated verification: verifying static files exist and serve cleanly.', { command: 'ls -la frontend/' });
            },
            () => {
                state.metrics.tokensPrompt += 1200;
                state.metrics.tokensCompletion += 880;
                state.dagNodes[4].status = 'COMPLETED';
                state.metrics.completedSteps = 5;
                state.metrics.status = 'COMPLETED';
                updateMetricsUI();
                renderDAG();
                appendTraceLog('SYSTEM', 'Task 10 execution completed successfully! All files created & verified.');
                stopDemoStream();
            }
        ];

        let stepIndex = 0;
        state.demoInterval = setInterval(() => {
            if (stepIndex < demoSequence.length) {
                demoSequence[stepIndex]();
                stepIndex++;
            } else {
                stopDemoStream();
            }
        }, 2200);
    }

    function stopDemoStream() {
        state.isDemoMode = false;
        if (state.demoInterval) {
            clearInterval(state.demoInterval);
            state.demoInterval = null;
        }
        dom.demoToggleBtn.classList.remove('btn-primary');
        dom.demoToggleBtn.classList.add('btn-secondary');
        dom.demoToggleBtn.textContent = 'Demo Stream';
    }

    // --- Model Provider Config Manager ---
    function loadProviderConfig() {
        const saved = localStorage.getItem('autoswe_provider_config');
        if (saved) {
            try {
                const config = JSON.parse(saved);
                dom.providerSelect.value = config.provider || 'gemini';
                dom.baseUrlInput.value = config.base_url || '';
                dom.apiKeyInput.value = config.api_key || '';
                dom.modelNameInput.value = config.model_name || 'gemini-3.6-flash';
                dom.tempInput.value = config.temperature || 0.2;
                dom.tempValue.textContent = config.temperature || 0.2;
            } catch (e) {}
        }
    }

    function saveProviderConfig() {
        const config = {
            provider: dom.providerSelect.value,
            base_url: dom.baseUrlInput.value.trim(),
            api_key: dom.apiKeyInput.value.trim(),
            model_name: dom.modelNameInput.value.trim() || 'gemini-3.6-flash',
            temperature: parseFloat(dom.tempInput.value)
        };

        localStorage.setItem('autoswe_provider_config', JSON.stringify(config));

        // POST to backend API
        let host = window.location.host;
        if (!host || window.location.protocol === 'file:') host = '127.0.0.1:8000';
        const apiUrl = `${window.location.protocol === 'https:' ? 'https:' : 'http:'}//${host}/api/v1/provider-config`;

        fetch(apiUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        })
        .then(res => res.json())
        .then(data => {
            appendTraceLog('SYSTEM', `Model Provider updated to: ${config.provider.toUpperCase()} (Model: ${config.model_name}, URL: "${config.base_url || 'Cloud Default'}")`);
            dom.providerModal.classList.add('hidden');
        })
        .catch(err => {
            appendTraceLog('SYSTEM', `Saved local provider config: ${config.provider.toUpperCase()} (${config.model_name})`);
            dom.providerModal.classList.add('hidden');
        });
    }

    function testProviderConnection() {
        const statusBox = dom.connectionStatusBox;
        statusBox.classList.remove('hidden', 'success', 'error', 'info');
        statusBox.classList.add('info');
        statusBox.textContent = 'Testing connection to model provider server...';

        const config = {
            provider: dom.providerSelect.value,
            base_url: dom.baseUrlInput.value.trim(),
            api_key: dom.apiKeyInput.value.trim(),
            model_name: dom.modelNameInput.value.trim() || 'gemini-3.6-flash',
            temperature: parseFloat(dom.tempInput.value)
        };

        let host = window.location.host;
        if (!host || window.location.protocol === 'file:') host = '127.0.0.1:8000';
        const testUrl = `${window.location.protocol === 'https:' ? 'https:' : 'http:'}//${host}/api/v1/provider-config/test`;

        fetch(testUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        })
        .then(res => res.json())
        .then(data => {
            statusBox.classList.remove('info');
            if (data.success) {
                statusBox.classList.add('success');
                statusBox.textContent = data.message;
                if (data.api_key && !dom.apiKeyInput.value) {
                    dom.apiKeyInput.value = data.api_key;
                }
                if (data.available_models && data.available_models.length > 0) {
                    dom.modelNameInput.value = data.available_models[0];
                }
            } else {
                statusBox.classList.add('error');
                statusBox.textContent = `${data.message} ${data.error_detail || ''}`;
            }
        })
        .catch(err => {
            statusBox.classList.remove('info');
            statusBox.classList.add('error');
            statusBox.textContent = `Connection error: ${err.message}`;
        });
    }


    // --- Helpers ---
    function escapeHtml(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
});

