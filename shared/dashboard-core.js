/**
 * dashboard-core.js — Shared UI runtime for all Universal AI dashboards.
 * Provides: API connectivity, theme toggle (light/dark), chat panel,
 * log streaming, sidebar injection, toast notifications, and live agent status.
 *
 * All 9 agent dashboards + root portal import this single file.
 * Version: 3.0.0 — Light/Dark mode + Chat
 */
(function () {
  'use strict';

  const API_URL = 'http://localhost:8000';
  const POLL_INTERVAL = 3000;

  // ── State ─────────────────────────────────────────────────────
  const state = {
    connected: false,
    agents: [],
    logs: [],
    activeAgent: null,
    theme: 'light',
    chatOpen: false,
    chatMessages: [],
  };

  // ── Theme Management ──────────────────────────────────────────
  function initTheme() {
    const saved = localStorage.getItem('uai-theme');
    if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      state.theme = 'dark';
      document.documentElement.classList.add('dark');
    } else {
      state.theme = 'light';
      document.documentElement.classList.remove('dark');
    }
    updateThemeToggleUI();
  }

  function toggleTheme() {
    state.theme = state.theme === 'dark' ? 'light' : 'dark';
    if (state.theme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
    localStorage.setItem('uai-theme', state.theme);
    updateThemeToggleUI();
    showToast('THEME: ' + state.theme.toUpperCase(), 'info');
  }

  function updateThemeToggleUI() {
    const toggles = document.querySelectorAll('.theme-toggle');
    toggles.forEach(t => {
      t.setAttribute('aria-label', state.theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
    });
  }

  // ── API Helpers ───────────────────────────────────────────────
  async function apiGet(path) {
    try {
      const res = await fetch(`${API_URL}${path}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      state.connected = false;
      updateConnectionUI(false);
      return null;
    }
  }

  async function apiPost(path, body) {
    try {
      const res = await fetch(`${API_URL}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      state.connected = false;
      updateConnectionUI(false);
      return null;
    }
  }

  // ── Connection UI ─────────────────────────────────────────────
  function updateConnectionUI(connected) {
    state.connected = connected;
    const indicator = document.getElementById('api-indicator');
    if (!indicator) return;
    if (connected) {
      indicator.className = 'status-dot online';
      indicator.title = 'API Connected';
    } else {
      indicator.className = 'status-dot offline';
      indicator.title = 'API Disconnected';
    }
  }

  async function checkConnection() {
    const data = await apiGet('/');
    if (data) {
      state.connected = true;
      state.agents = data.active_agents || [];
      updateConnectionUI(true);
    } else {
      updateConnectionUI(false);
    }
  }

  // ── Toast Notifications ───────────────────────────────────────
  function showToast(msg, type = 'info') {
    const container = document.getElementById('toast-container') || createToastContainer();
    const el = document.createElement('div');
    const colors = {
      info: 'border-[#3B82F6]/30 bg-[#3B82F6]/5 text-[#3B82F6]',
      warn: 'border-amber-500/30 bg-amber-500/5 text-amber-500',
      error: 'border-red-500/30 bg-red-500/5 text-red-500',
      success: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400',
    };
    el.className = 'toast ' + (colors[type] || colors.info);
    el.textContent = msg;
    container.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 300);
    }, 3500);
  }

  function createToastContainer() {
    const container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
    return container;
  }

  // ── Chat Panel ────────────────────────────────────────────────
  function initChat(agentName, agentIcon) {
    // Create FAB
    const fab = document.createElement('button');
    fab.className = 'chat-fab';
    fab.innerHTML = '<i class="ti ti-messages"></i>';
    fab.setAttribute('aria-label', 'Open chat');
    fab.addEventListener('click', toggleChat);
    document.body.appendChild(fab);

    // Create chat panel
    const panel = document.createElement('div');
    panel.className = 'chat-panel';
    panel.id = 'chat-panel';
    panel.innerHTML = `
      <div class="chat-header">
        <div class="chat-header-left">
          <div class="chat-agent-icon">${agentIcon || '<i class="ti ti-messages"></i>'}</div>
          <div>
            <div class="chat-agent-name">${agentName || 'Universal AI'}</div>
            <div class="chat-agent-subtitle">Ask me anything</div>
          </div>
        </div>
        <button class="btn-icon" id="chat-close" aria-label="Close chat">
          <i class="ti ti-x"></i>
        </button>
      </div>
      <div class="chat-messages" id="chat-messages">
        <div class="chat-msg agent">
          Hello! I'm the ${agentName || 'Universal AI'} assistant. How can I help you today?
          <div class="msg-time">${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>
        </div>
      </div>
      <div class="chat-input-area">
        <input type="text" class="input" id="chat-input" placeholder="Type a message..." />
        <button class="btn btn-primary" id="chat-send">
          <i class="ti ti-send"></i>
        </button>
      </div>
    `;
    document.body.appendChild(panel);

    // Wire events
    document.getElementById('chat-close').addEventListener('click', () => {
      closeChat();
    });
    const chatInput = document.getElementById('chat-input');
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage(agentName);
      }
    });
    document.getElementById('chat-send').addEventListener('click', () => {
      sendChatMessage(agentName);
    });

    // Global keyboard shortcut for chat
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'j') {
        e.preventDefault();
        toggleChat();
      }
    });
  }

  function toggleChat() {
    state.chatOpen = !state.chatOpen;
    const panel = document.getElementById('chat-panel');
    if (panel) {
      panel.classList.toggle('open', state.chatOpen);
      if (state.chatOpen) {
        document.getElementById('chat-input')?.focus();
      }
    }
  }

  function closeChat() {
    state.chatOpen = false;
    const panel = document.getElementById('chat-panel');
    if (panel) panel.classList.remove('open');
  }

  function sendChatMessage(agentName) {
    const input = document.getElementById('chat-input');
    if (!input || !input.value.trim()) return;
    const text = input.value.trim();
    input.value = '';

    addChatMessage('user', text);

    // Simulate agent response (real API can be plugged in here)
    setTimeout(() => {
      const responses = [
        `I've analyzed your query about "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}" and here's what I found.`,
        `That's an interesting point. Let me fetch the relevant data from the ${agentName} module.`,
        `Based on the current ecosystem state, I can see several relevant patterns related to your question.`,
        `I'm processing your request through the ${agentName} intelligence engine. Here are the results.`,
        `Good question! The ${agentName} agent has been tracking this area closely. Let me summarize the key insights.`,
      ];
      addChatMessage('agent', responses[Math.floor(Math.random() * responses.length)]);
    }, 600 + Math.random() * 1200);
  }

  function addChatMessage(type, text) {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'chat-msg ' + type;
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    el.innerHTML = `${text}<div class="msg-time">${time}</div>`;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  // ── Log Streaming ─────────────────────────────────────────────
  function createLogContainer(containerId, filterAgent = null, maxLines = 15) {
    const container = document.getElementById(containerId);
    if (!container) return { push: () => { }, poll: () => { } };

    function push(log) {
      const el = document.createElement('div');
      el.className = 'terminal-line';
      const time = log.time || new Date().toLocaleTimeString([], { hour12: false });
      const agent = log.agent || 'SYSTEM';
      const type = log.type || 'INFO';
      const msg = log.msg || log;

      let typeClass = 'info';
      if (type === 'WARN' || type === 'ERROR') typeClass = 'error';
      if (type === 'INFO') typeClass = 'info';
      if (type === 'SYNC' || type === 'BOOT') typeClass = 'sync';
      if (type === 'OK' || type === 'SAFE') typeClass = 'success';
      if (type === 'WRITE' || type === 'COMMIT') typeClass = 'info';
      el.className += ' ' + typeClass;
      el.innerHTML = `<span class="time">[${time}]</span> <span class="agent-tag">[${agent}]</span> ${msg}`;
      container.appendChild(el);
      while (container.children.length > maxLines) container.firstElementChild.remove();
      container.scrollTop = container.scrollHeight;
    }

    async function poll() {
      if (!state.connected) return;
      const data = await apiGet('/logs?limit=10');
      if (data && data.length > 0) {
        const filtered = filterAgent ? data.filter(l => l.agent === filterAgent.toUpperCase()) : data;
        container.innerHTML = '';
        filtered.reverse().forEach(l => push(l));
      }
    }

    return { push, poll };
  }

  // ── Keyboard Navigation ───────────────────────────────────────
  function initKeyboardNav(agentOrder) {
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        showToast('COMMAND PALETTE: NOT YET IMPLEMENTED', 'info');
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === '[' || e.key === ']')) {
        e.preventDefault();
        const current = state.activeAgent;
        if (!current || !agentOrder) return;
        const idx = agentOrder.indexOf(current);
        const dir = e.key === '[' ? -1 : 1;
        const next = agentOrder[(idx + dir + agentOrder.length) % agentOrder.length];
        showToast('NAVIGATING TO ' + next.toUpperCase(), 'info');
        setTimeout(() => {
          window.location.href = getAgentUrl(next);
        }, 400);
      }
      // Toggle theme: Ctrl+Shift+T
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'T') {
        e.preventDefault();
        toggleTheme();
      }
    });
  }

  function getAgentUrl(name) {
    const map = {
      chronicle: '../../Chronicle/dashboard/index.html',
      oracle: '../../Oracle/dashboard/index.html',
      nexus: '../../Nexus/dashboard/index.html',
      pulse: '../../Pulse/dashboard/index.html',
      atlas: '../../Atlas/dashboard/index.html',
      forge: '../../Forge/dashboard/index.html',
      genesis: '../../Genesis/dashboard/index.html',
      aegis: '../../Aegis/dashboard/index.html',
      sentinel: '../../Sentinel/dashboard/index.html',
    };
    return map[name] || `../../${name.charAt(0).toUpperCase() + name.slice(1)}/dashboard/index.html`;
  }

  // ── Agent Status Polling ──────────────────────────────────────
  async function pollAgentStatus(agentName) {
    if (!state.connected) return null;
    return await apiGet(`/agents/${agentName}/status`);
  }

  // ── Gauge Animation ───────────────────────────────────────────
  function animateGauge(selector, value, max, duration) {
    const el = document.querySelector(selector);
    if (!el) return;
    const circumference = parseFloat(el.getAttribute('stroke-dasharray') || '283');
    const target = circumference * (1 - value / max);
    if (typeof gsap !== 'undefined') {
      gsap.to(el, { strokeDashoffset: target, duration: duration || 1.5, ease: 'power2.out' });
    } else {
      el.style.transition = `stroke-dashoffset ${duration || 1.5}s ease`;
      el.style.strokeDashoffset = target;
    }
  }

  // ── Public API ────────────────────────────────────────────────
  window.UniversalAI = {
    API_URL,
    apiGet,
    apiPost,
    checkConnection,
    createLogContainer,
    showToast,
    initKeyboardNav,
    pollAgentStatus,
    animateGauge,
    state,
    updateConnectionUI,
    initTheme,
    toggleTheme,
    initChat,
    sendChatMessage,
    addChatMessage,
    toggleChat,
    closeChat,
  };

  // ── Auto-init on load ─────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    checkConnection();
    setInterval(checkConnection, 15000);

    // Wire up any theme-toggle buttons in the page
    document.querySelectorAll('.theme-toggle').forEach(btn => {
      btn.addEventListener('click', toggleTheme);
    });
  });
})();
