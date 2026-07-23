/**
 * api.js — Universe Ecosystem v5
 * Backend-first. No demo data. Clear offline states.
 * Chat API: POST /agents/{id}/chat  body:{message:"..."}  response:{response:"..."}
 */

// ── CONNECTION STATE ──────────────────────────────────────────────────────────
const ConnectionState = {
  online: false,
  latency: null,
  lastCheck: null,
  misses: 0,
  listeners: [],

  set(online, latency, opts = {}) {
    if (!online && opts.tolerateMiss && this.online && this.misses < 2) {
      this.misses += 1;
      this.lastCheck = Date.now();
      this._updateBadge('Reconnecting');
      return;
    }
    if (online) this.misses = 0;
    const changed = this.online !== online;
    this.online = online;
    this.latency = latency;
    this.lastCheck = Date.now();
    if (changed) this.listeners.forEach(fn => fn(online, latency));
    this._updateBadge();
  },

  onChange(fn) { this.listeners.push(fn); },

  _updateBadge(label) {
    const badge = document.getElementById('connBadge');
    if (!badge) return;
    if (this.online) {
      badge.className = 'api-badge api-online';
      badge.innerHTML = `<span class="dot dot-green"></span> ${label || 'Live'}${this.latency ? ` · ${this.latency}ms` : ''}`;
    } else {
      badge.className = 'api-badge api-offline';
      badge.innerHTML = `<span class="dot dot-red"></span> Offline`;
    }
  },
};

// ── CORE FETCH ────────────────────────────────────────────────────────────────
async function apiGet(url, { timeout = 8000, retries = 1 } = {}) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    const t0 = performance.now();
    try {
      const res = await fetch(url, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      ConnectionState.set(true, Math.round(performance.now() - t0));
      return data;
    } catch (err) {
      clearTimeout(timer);
      if (attempt === retries) {
        console.warn(`[API] GET failed:`, url, err.message);
        ConnectionState.set(false, null);
        return null;
      }
      await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  return null;
}

async function apiPost(url, body, { timeout = 30000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const t0 = performance.now();
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    if (!res.ok) {
      const errText = await res.text().catch(() => '');
      throw new Error(`HTTP ${res.status}${errText ? ': ' + errText.slice(0, 200) : ''}`);
    }
    const data = await res.json();
    ConnectionState.set(true, Math.round(performance.now() - t0));
    return data;
  } catch (err) {
    clearTimeout(timer);
    console.warn('[API] POST failed:', url, err.message);
    ConnectionState.set(false, null);
    return null;
  }
}

// ── AGENT CHAT ────────────────────────────────────────────────────────────────
/**
 * Send a message to an agent.
 * Returns the response string, or null if backend is offline.
 * The caller is responsible for showing an offline error message.
 */
async function chatAgent(agentId, message) {
  const url = API.agentChat(agentId);
  const data = await apiPost(url, { message }, { timeout: 60000 });
  if (!data) return null; // backend offline — caller shows error

  // Extract response text from various possible field names
  return data.response
    ?? data.message
    ?? data.reply
    ?? data.text
    ?? data.result
    ?? data.output
    ?? (typeof data === 'string' ? data : null)
    ?? JSON.stringify(data);
}

// ── AGENT DATA ────────────────────────────────────────────────────────────────
/**
 * Fetch live data for an agent's dashboard panel.
 * Returns data object or null if backend is offline.
 */
async function fetchAgentData(agentId) {
  const data = await apiGet(API.agentData(agentId), { timeout: 8000, retries: 1 });
  return data; // null = offline
}

// ── AGENT LIST ────────────────────────────────────────────────────────────────
async function fetchAgents() {
  return await apiGet(API.agents, { timeout: 8000, retries: 2 });
}

// ── CONNECTION POLLING ────────────────────────────────────────────────────────
let _connPollTimer = null;

function startConnectionPolling(intervalMs = 30000) {
  const check = async () => {
    const t0 = performance.now();
    const data = await apiGet(API.health, { timeout: 8000, retries: 1 });
    const latency = Math.round(performance.now() - t0);
    ConnectionState.set(!!data, data ? latency : null, { tolerateMiss: true });
  };
  check();
  _connPollTimer = setInterval(check, intervalMs);
}

function stopConnectionPolling() {
  if (_connPollTimer) { clearInterval(_connPollTimer); _connPollTimer = null; }
}

// ── AUTO-REFRESH ──────────────────────────────────────────────────────────────
function autoRefresh(fetchFn, interval) {
  fetchFn();
  const timer = setInterval(fetchFn, interval);
  return { stop: () => clearInterval(timer) };
}

// ── SKELETON / ERROR / EMPTY HELPERS ─────────────────────────────────────────
function showSkeleton(containerId, rows = 3) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = Array.from({ length: rows }, () =>
    `<div class="skeleton skeleton-row" style="margin-bottom:6px"></div>`
  ).join('');
}

function showError(containerId, message, onRetry) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="error-state">
      <span class="error-icon">⚠️</span>
      <div class="error-body">
        <div class="error-title">Failed to load data</div>
        <div class="error-desc">${message || 'Backend may be offline.'}</div>
      </div>
      ${onRetry ? `<button class="retry-btn" onclick="(${onRetry.toString()})()">Retry</button>` : ''}
    </div>`;
}

function showOfflinePanel(containerId, agentName) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="offline-panel">
      <div class="offline-icon">🔌</div>
      <div class="offline-title">Reconnecting</div>
      <div class="offline-desc">Live ${agentName || 'agent'} data will return automatically when the backend responds.</div>
    </div>`;
}

function showEmpty(containerId, title = 'No data yet', desc = 'Data will appear when the agent is active.') {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">📭</div>
      <div class="empty-title">${title}</div>
      <div class="empty-desc">${desc}</div>
    </div>`;
}
