/**
 * config.js — Universe Ecosystem v5
 * Backend-first. No demo mode. Clear offline states.
 * API contract: POST /agents/{id}/chat  body: {message:"..."}  response: {response:"..."}
 */

// ── BASE URL ──────────────────────────────────────────────────────────────────
const BASE_URL = (() => {
  if (typeof window.UNIVERSE_API_BASE !== 'undefined') return window.UNIVERSE_API_BASE.replace(/\/$/, '');
  const stored = localStorage.getItem('universe-api-base');
  if (stored && /^https?:\/\//i.test(stored)) return stored.replace(/\/$/, '');
  const origin = window.location.origin;
  if (!origin || origin === 'null' || window.location.protocol === 'file:') {
    return 'http://localhost:3000';
  }
  const port = window.location.port;
  if (port === '5500' || port === '5501' || port === '8080' || port === '5173') {
    return 'http://localhost:3000';
  }
  return origin.replace(/\/$/, '');
})();

const CONFIG = {
  API_BASE: BASE_URL,
};
window.CONFIG = CONFIG;
window.API_BASE = BASE_URL;

// Auto-probe fallback between localhost:3000 (Express) and localhost:8000 (FastAPI) when running locally
(async function autoProbeLocalBackend() {
  try {
    const res = await fetch(`${window.API_BASE}/health`, { signal: AbortSignal.timeout(2500) });
    if (res.ok) return;
  } catch (e) {}
  for (const candidate of ['http://localhost:3000', 'http://localhost:8000', 'http://127.0.0.1:3000', 'http://127.0.0.1:8000']) {
    if (candidate === window.API_BASE) continue;
    try {
      const r = await fetch(`${candidate}/health`, { signal: AbortSignal.timeout(2000) });
      if (r.ok) {
        window.API_BASE = candidate;
        window.CONFIG.API_BASE = candidate;
        API.base = candidate;
        API.agents = `${candidate}/agents`;
        API.health = `${candidate}/health`;
        API.ecosystemChat = `${candidate}/ecosystem/chat`;
        break;
      }
    } catch (e) {}
  }
})();

// ── ENDPOINTS ─────────────────────────────────────────────────────────────────
const API = {
  base:        BASE_URL,
  agents:      `${BASE_URL}/agents`,
  health:      `${BASE_URL}/health`,

  agentData:   (id) => `${BASE_URL}/agents/${id}/data`,
  agentChat:   (id) => `${BASE_URL}/agents/${id}/chat`,
  ecosystemChat: `${BASE_URL}/ecosystem/chat`,

  ws:          (id) => `${BASE_URL.replace(/^http/, 'ws')}/ws/${id}`,
};

// ── AGENT REGISTRY ────────────────────────────────────────────────────────────
const AGENTS = {
  nexus:     { id:'nexus',     name:'Nexus',     role:'Coordinator Hub',       icon:'⚡', color:'#6366F1', colorRgb:'99,102,241',   page:'nexus.html',     desc:'Routes queries to the right agents, synthesizes responses, and monitors system health.',       tags:['Orchestration','Routing','Health'],      placeholder:'Ask the ecosystem anything… e.g. "query what is the market outlook"' },
  oracle:    { id:'oracle',    name:'Oracle',    role:'Prediction Engine',     icon:'📈', color:'#10B981', colorRgb:'16,185,129',   page:'oracle.html',    desc:'Generates trading signals and manages portfolio positions across forex, crypto, and commodities.', tags:['Signals','Portfolio','Forex'],           placeholder:'e.g. "query what are the current trading signals"' },
  atlas:     { id:'atlas',     name:'Atlas',     role:'Research Engine',       icon:'🗺️', color:'#0EA5E9', colorRgb:'14,165,233',   page:'atlas.html',     desc:'Deep research agent. Queries Wikipedia, DuckDuckGo, and economic databases with cited evidence.', tags:['Research','Wikipedia','Evidence'],       placeholder:'e.g. "query what is quantitative easing"' },
  chronicle: { id:'chronicle', name:'Chronicle', role:'Memory Engine',         icon:'📜', color:'#8B5CF6', colorRgb:'139,92,246',   page:'chronicle.html', desc:'Long-term memory and pattern recognition. Stores summaries and surfaces historical analogues.',    tags:['Memory','Patterns','History'],           placeholder:'e.g. "query what happened last week"' },
  aegis:     { id:'aegis',     name:'Aegis',     role:'Governance & Security', icon:'🛡️', color:'#F97316', colorRgb:'249,115,22',   page:'aegis.html',     desc:'Monitors compliance, enforces governance rules, and blocks unauthorized actions.',               tags:['Governance','Compliance','Security'],    placeholder:'e.g. "query what are the current risk levels"' },
  sentinel:  { id:'sentinel',  name:'Sentinel',  role:'News Intelligence',     icon:'📡', color:'#F59E0B', colorRgb:'245,158,11',   page:'sentinel.html',  desc:'Monitors global news feeds, detects market-moving events, and classifies headlines by impact.',   tags:['News','Events','Impact'],                placeholder:'e.g. "report latest market news"' },
  forge:     { id:'forge',     name:'Forge',     role:'Automation Engine',     icon:'⚙️', color:'#64748B', colorRgb:'100,116,139',  page:'forge.html',     desc:'Manages automated workflows, scheduled tasks, and job execution pipelines.',                    tags:['Automation','Workflows','Jobs'],         placeholder:'e.g. "query what workflows are running"' },
  genesis:   { id:'genesis',   name:'Genesis',   role:'Strategy Engine',       icon:'🌱', color:'#14B8A6', colorRgb:'20,184,166',   page:'genesis.html',   desc:'Generates and evolves trading strategies, sets goals, and provides market outlook.',             tags:['Strategy','Goals','Evolution'],          placeholder:'e.g. "query what is the current strategy"' },
  pulse:     { id:'pulse',     name:'Pulse',     role:'Social Sentiment',      icon:'💓', color:'#EC4899', colorRgb:'236,72,153',   page:'pulse.html',     desc:'Tracks social media sentiment across Reddit, Twitter, and financial communities.',               tags:['Sentiment','Social','Reddit'],           placeholder:'e.g. "query what is the social sentiment on Bitcoin"' },
  phantom:   { id:'phantom',   name:'Phantom',   role:'Stealth Agent',         icon:'👻', color:'#6B7280', colorRgb:'107,114,128',  page:'index.html',     desc:'Stealth operations agent.',                                                                       tags:['Stealth'],                               placeholder:'e.g. "query phantom status"' },
};

const AGENT_ORDER = ['nexus','oracle','atlas','chronicle','aegis','sentinel','forge','genesis','pulse'];

// ── REFRESH INTERVALS ─────────────────────────────────────────────────────────
const REFRESH_INTERVALS = {
  oracle:30000, sentinel:45000, pulse:45000,
  atlas:60000, chronicle:60000, nexus:30000,
  aegis:60000, forge:60000, genesis:60000,
};

// ── CHART DEFAULTS ────────────────────────────────────────────────────────────
const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend:{ display:false }, tooltip:{ mode:'index', intersect:false } },
  scales: {
    x: { grid:{ display:false }, ticks:{ color:'#6B6B7B', font:{ size:10 } } },
    y: { grid:{ color:'rgba(128,128,128,0.1)' }, ticks:{ color:'#6B6B7B', font:{ size:10 } } },
  },
  animation: { duration:600 },
};
