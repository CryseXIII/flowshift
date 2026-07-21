const BASE = '';

async function get(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: r.statusText }));
    throw new Error(err.error || err.message || r.statusText);
  }
  return r.json();
}

async function post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: r.statusText }));
    throw new Error(err.error || err.message || r.statusText);
  }
  return r.json();
}

async function del(path) {
  const r = await fetch(`${BASE}${path}`, { method: 'DELETE' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: r.statusText }));
    throw new Error(err.error || err.message || r.statusText);
  }
  return r.json();
}

export function getStatus() {
  return get('/api/status');
}

export function getSettings() {
  return get('/api/settings');
}

export function saveSettings(settings) {
  return post('/api/settings', settings);
}

export function getUpdateStatus() {
  return get('/api/update/status');
}

export function checkForUpdates() {
  return post('/api/update/check', {});
}

export function downloadUpdate() {
  return post('/api/update/download', {});
}

export function installUpdate() {
  return post('/api/update/install', {});
}

export function saveUpdateSettings(settings) {
  return post('/api/update/settings', settings);
}

export function getPeers() {
  return get('/api/peers');
}

export function getClipboardItems(profile) {
  const q = profile ? `?profile=${encodeURIComponent(profile)}` : '';
  return get(`/api/clipboard/items${q}`);
}

export function getClipboardItem(profile, itemId) {
  return get(`/api/clipboard/item/${encodeURIComponent(itemId)}?profile=${encodeURIComponent(profile)}`);
}

export function pasteItem(profile, itemId) {
  return post(`/api/clipboard/item/${encodeURIComponent(itemId)}/paste?profile=${encodeURIComponent(profile)}`);
}

export function deleteItem(profile, itemId) {
  return del(`/api/clipboard/item/${encodeURIComponent(itemId)}?profile=${encodeURIComponent(profile)}`);
}

export function pinItem(profile, itemId, pinned) {
  return post(`/api/clipboard/item/${encodeURIComponent(itemId)}/pin?profile=${encodeURIComponent(profile)}`, { pinned });
}

export function requestItem(profile, itemId) {
  return post(`/api/clipboard/item/${encodeURIComponent(itemId)}/request?profile=${encodeURIComponent(profile)}`);
}

export function syncClipboard(profile) {
  return post(`/api/clipboard/sync?profile=${encodeURIComponent(profile)}`);
}

export function clearClipboard(profile) {
  return post(`/api/clipboard/clear?profile=${encodeURIComponent(profile)}`);
}

export function activateForwarding(profile) {
  return post('/api/forwarding/activate', { profile });
}

export function deactivateForwarding() {
  return post('/api/forwarding/deactivate');
}

export function toggleForwarding(profile) {
  return post('/api/forwarding/toggle', { profile });
}

export function getClipboardProgress() {
  return get('/api/clipboard/progress');
}

export function getThumbnail(profile, itemId) {
  return get(`/api/clipboard/thumbnail/${encodeURIComponent(itemId)}?profile=${encodeURIComponent(profile)}`);
}

export function getHotkeys() {
  return get('/api/hotkeys');
}

export function getDisplayLayout() {
  return get('/api/display/layout');
}

export function saveDisplayLayout(layout) {
  return post('/api/display/layout', { layout });
}

export function subscribeSSE(onEvent) {
  const es = new EventSource(`${BASE}/api/events`);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch { }
  };
  es.onerror = () => { };
  return () => es.close();
}

export function addPeer(peer) {
  return post('/api/peers/add', peer);
}

export function removePeer(index, name, host) {
  return post('/api/peers/remove', { index, name, host });
}

export function scanNetwork(timeout) {
  return post('/api/peers/scan', { timeout: timeout || 2.0 });
}

export function getAutoStart() {
  return get('/api/auto-start');
}

export function setAutoStart(enabled) {
  return post('/api/auto-start', { enabled });
}

export function shutdownApp() {
  return post('/api/shutdown');
}

export function editPeer(index, data) {
  return post('/api/peers/edit', { index, ...data });
}

export function pingPeer(peerRef) {
  return post('/api/peers/ping', { peer: peerRef });
}

export function getDiagnostics() {
  return post('/api/diagnostics');
}

export function showDiagnosticOverlay(mode) {
  return post('/api/overlay/show', { mode, payload: { diagnostic: true } });
}

export function hideDiagnosticOverlay() {
  return post('/api/overlay/hide');
}

export function pingOverlay() {
  return post('/api/overlay/ping');
}

export function injectType(text) {
  return post('/api/inject/type', { text });
}

export function injectKey(vk, action) {
  return post('/api/inject/key', { vk, action: action || 'tap' });
}

export function getWebguiConfig() {
  return get('/api/webgui/config');
}

export function setWebguiConfig(data) {
  return post('/api/webgui/config', data);
}

export function restartService() {
  return post('/api/restart');
}
