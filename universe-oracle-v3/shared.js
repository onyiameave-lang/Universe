/* ── UNIVERSE ORACLE v3 — SHARED JS ─────────────────────────────────────────── */

// ── THEME ─────────────────────────────────────────────────────────────────────
(function() {
  const t = localStorage.getItem('uo-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', t);
})();

function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('uo-theme', next);
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = next === 'dark' ? '☀️ Light' : '🌙 Dark';
  if (typeof onThemeChange === 'function') onThemeChange(next);
}

document.addEventListener('DOMContentLoaded', () => {
  const t = localStorage.getItem('uo-theme') || 'dark';
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = t === 'dark' ? '☀️ Light' : '🌙 Dark';
});

// ── KPI COUNTER ANIMATION ─────────────────────────────────────────────────────
function animateKPIs() {
  document.querySelectorAll('[data-target]').forEach(el => {
    const target = parseFloat(el.dataset.target);
    const prefix = el.dataset.prefix || '';
    const suffix = el.dataset.suffix || '';
    const decimals = parseInt(el.dataset.decimals || '0');
    const duration = 1200;
    const start = performance.now();
    function step(now) {
      const p = Math.min((now - start) / duration, 1);
      const ease = 1 - Math.pow(1 - p, 3);
      const val = target * ease;
      el.textContent = prefix + val.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ',') + suffix;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}
document.addEventListener('DOMContentLoaded', animateKPIs);

// ── CHAT PANEL ────────────────────────────────────────────────────────────────
let chatOpen = false;
function toggleChat() {
  chatOpen = !chatOpen;
  const panel = document.getElementById('chatPanel');
  if (panel) panel.classList.toggle('open', chatOpen);
}

function sendPanelMsg() {
  const input = document.getElementById('panelInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = 'auto';

  const msgs = document.getElementById('panelMessages');
  // User bubble
  const userDiv = document.createElement('div');
  userDiv.className = 'panel-msg user-msg';
  userDiv.innerHTML = `<div class="panel-bubble">${escHtml(text)}</div>`;
  msgs.appendChild(userDiv);
  msgs.scrollTop = msgs.scrollHeight;

  // Typing
  const typingDiv = document.createElement('div');
  typingDiv.className = 'panel-msg agent-msg';
  typingDiv.innerHTML = `<div class="panel-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
  msgs.appendChild(typingDiv);
  msgs.scrollTop = msgs.scrollHeight;

  setTimeout(() => {
    typingDiv.remove();
    const resp = typeof getAgentResponse === 'function' ? getAgentResponse() : 'Processing your query...';
    const agentDiv = document.createElement('div');
    agentDiv.className = 'panel-msg agent-msg';
    agentDiv.innerHTML = `<div class="panel-bubble">${escHtml(resp)}</div>`;
    msgs.appendChild(agentDiv);
    msgs.scrollTop = msgs.scrollHeight;
  }, 1200);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('panelInput');
  if (ta) {
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPanelMsg(); }
    });
    ta.addEventListener('input', () => {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, 80) + 'px';
    });
  }
});

// ── CHART DEFAULTS ────────────────────────────────────────────────────────────
function chartDefaults(theme) {
  const isDark = (theme || localStorage.getItem('uo-theme') || 'dark') === 'dark';
  return {
    gridColor: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.06)',
    tickColor: isDark ? '#64748B' : '#94A3B8',
    tooltipBg: isDark ? '#1E2530' : '#FFFFFF',
    tooltipText: isDark ? '#E2E8F0' : '#0F172A',
  };
}
