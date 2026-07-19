/**
 * api-config.js — Universe Oracle v7
 * ─────────────────────────────────────────────────────────────────────────────
 * Single source of truth for the FastAPI backend URL and all shared fetch
 * helpers used by every agent page.
 *
 * DESTINATION: Oracle/frontend/api-config.js
 *              (served by FastAPI StaticFiles at /Oracle/api-config.js)
 *
 * HOW TO CONFIGURE:
 *   • Development (local):  ORACLE_API_BASE = 'http://localhost:8000'
 *   • Production (VPS):     ORACLE_API_BASE = 'https://your-domain.com'
 *   • The value is read from window.ORACLE_API_BASE if set by the server,
 *     otherwise falls back to the constant below.
 * ─────────────────────────────────────────────────────────────────────────────
 */

// ── BASE URL ─────────────────────────────────────────────────────────────────
// Override by setting window.ORACLE_API_BASE before this script loads,
// or by editing the constant below for your deployment.
const API_BASE = (typeof window.ORACLE_API_BASE !== 'undefined')
  ? window.ORACLE_API_BASE.replace(/\/$/, '')
  : 'http://localhost:8000';

// ── ENDPOINTS ─────────────────────────────────────────────────────────────────
const API = {
  status:       `${API_BASE}/api/status`,
  agents:       `${API_BASE}/agents`,
  logs:         `${API_BASE}/logs`,
  agentStatus:  (name) => `${API_BASE}/agents/${name}/status`,
  agentQuery:   (name) => `${API_BASE}/agents/${name}/query`,
};

// ── FETCH HELPERS ─────────────────────────────────────────────────────────────

/**
 * GET a JSON endpoint. Returns parsed JSON or null on error.
 * @param {string} url
 * @param {number} [timeoutMs=8000]
 */
async function apiGet(url, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    clearTimeout(timer);
    console.warn('[Oracle API] GET failed:', url, err.message);
    return null;
  }
}

/**
 * POST JSON to an endpoint. Returns parsed JSON or null on error.
 * @param {string} url
 * @param {object} body
 * @param {number} [timeoutMs=15000]
 */
async function apiPost(url, body, timeoutMs = 15000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    clearTimeout(timer);
    console.warn('[Oracle API] POST failed:', url, err.message);
    return null;
  }
}

/**
 * Query an agent via POST /agents/{name}/query.
 * @param {string} agentName  e.g. 'oracle'
 * @param {string} prompt     natural-language query
 * @param {number} [timeoutMs=20000]
 * @returns {Promise<string>} agent response text, or error message
 */
async function queryAgent(agentName, prompt, timeoutMs = 20000) {
  const data = await apiPost(API.agentQuery(agentName), { prompt }, timeoutMs);
  if (!data) return `⚠️ ${agentName} is offline or unreachable.`;
  return data.response ?? data.message ?? JSON.stringify(data);
}

/**
 * Execute a specific task on an agent via POST /agents/{name}/query.
 * The backend's query endpoint passes the prompt as context.query;
 * for structured tasks we embed the task name in the prompt so the
 * agent's execute() can parse it.
 *
 * @param {string} agentName
 * @param {string} task       e.g. 'portfolio.status', 'trade.signal'
 * @param {object} [extra]    extra context fields merged into prompt JSON
 */
async function agentTask(agentName, task, extra = {}) {
  // Encode as a structured prompt the backend can parse
  const prompt = JSON.stringify({ _task: task, ...extra });
  return apiPost(API.agentQuery(agentName), { prompt });
}

// ── CONNECTION STATUS BADGE ───────────────────────────────────────────────────
/**
 * Polls /api/status and updates a DOM element with id="apiStatusBadge".
 * Call once on DOMContentLoaded.
 */
async function initConnectionBadge() {
  const badge = document.getElementById('apiStatusBadge');
  if (!badge) return;

  const check = async () => {
    const data = await apiGet(API.status, 4000);
    if (data && data.status === 'online') {
      badge.textContent = '🟢 API Online';
      badge.className = 'api-badge api-online';
    } else {
      badge.textContent = '🔴 API Offline';
      badge.className = 'api-badge api-offline';
    }
  };

  await check();
  setInterval(check, 30_000); // re-check every 30 s
}

document.addEventListener('DOMContentLoaded', initConnectionBadge);
