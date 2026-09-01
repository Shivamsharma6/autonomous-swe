// Central authenticated API client for the control plane.

const TOKEN_KEY = 'autoswe.adminToken';

let onUnauthorized = null;

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(value) {
  sessionStorage.setItem(TOKEN_KEY, String(value || ''));
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

export async function api(path, options = {}, token = getToken()) {
  if (!token) {
    if (onUnauthorized) onUnauthorized();
    throw new Error('Operator authentication is required.');
  }
  const headers = new Headers(options.headers || {});
  headers.set('Authorization', `Bearer ${token}`);
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    if (onUnauthorized && token === getToken()) onUnauthorized();
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

// WebSocket factory that authenticates through the sec-websocket-protocol
// channel — query-string tokens are rejected by the control plane, and
// browsers cannot attach Authorization headers to WebSocket handshakes.
export function connectTaskSocket(projectId, taskId, { onEvent, onState }) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const path = `/api/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/events`;
  let socket;
  try {
    socket = new WebSocket(`${protocol}//${window.location.host}${path}`, [getToken()]);
  } catch (_) {
    if (onState) onState('error');
    return null;
  }
  socket.onopen = () => { if (onState) onState('open'); };
  socket.onmessage = (event) => {
    try {
      if (onEvent) onEvent(JSON.parse(event.data));
    } catch (_) { /* malformed frame */ }
  };
  socket.onerror = () => { if (onState) onState('error'); };
  socket.onclose = () => { if (onState) onState('close'); };
  return socket;
}
