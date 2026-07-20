/**
 * shared.js — Universe Oracle v2
 * Theme toggle, chat panel (live /agents/{name}/chat), connection badge,
 * KPI counter animation, conf-bar animation.
 */

// ── THEME ────────────────────────────────────────────────────────────────────
(function () {
  const saved = localStorage.getItem('uo-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = saved === 'dark' ? '☀️ Light' : '🌙 Dark';
})();

function toggleTheme() {
  const html  = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  const next   = isDark ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('uo-theme', next);
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = next === 'dark' ? '☀️ Light' : '🌙 Dark';
}

// ── KPI COUNTER ANIMATION ────────────────────────────────────────────────────
function animateKPI(el, target, prefix, suffix, decimals) {
  prefix   = prefix   || '';
  suffix   = suffix   || '';
  decimals = decimals || 0;
  const duration = 1200;
  const start    = performance.now();
  (function tick(now) {
    const t   = Math.min((now - start) / duration, 1);
    const val = target * (1 - Math.pow(1 - t, 3));
    el.textContent = prefix + val.toFixed(decimals) + suffix;
    if (t < 1) requestAnimationFrame(tick);
  })(start);
}

// Auto-animate any [data-target] KPI values on load
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-target]').forEach(function (el) {
    const target   = parseFloat(el.dataset.target) || 0;
    const prefix   = el.dataset.prefix   || '';
    const suffix   = el.dataset.suffix   || '';
    const decimals = parseInt(el.dataset.decimals || '0', 10);
    animateKPI(el, target, prefix, suffix, decimals);
  });
});

// ── CONF-BAR ANIMATION ───────────────────────────────────────────────────────
var _animatedBars = new WeakSet();
function animateConfBars() {
  document.querySelectorAll('.conf-bar-fill').forEach(function (bar) {
    if (_animatedBars.has(bar)) return;
    _animatedBars.add(bar);
    var target = parseFloat(bar.dataset.target || bar.style.width) || 0;
    bar.style.width = '0%';
    requestAnimationFrame(function () {
      bar.style.transition = 'width 0.9s cubic-bezier(0.4,0,0.2,1)';
      bar.style.width = target + '%';
    });
  });
}
document.addEventListener('DOMContentLoaded', function () {
  animateConfBars();
  var obs = new MutationObserver(animateConfBars);
  obs.observe(document.body, { childList: true, subtree: true });
});

// ── CHAT PANEL ───────────────────────────────────────────────────────────────
var chatOpen = false;

function toggleChat() {
  chatOpen = !chatOpen;
  var panel = document.getElementById('chatPanel');
  if (panel) panel.classList.toggle('open', chatOpen);
}

/**
 * universalSendMsg — sends a message to POST /agents/{name}/chat
 */
async function universalSendMsg(agentName, inputId, messagesId) {
  var input    = document.getElementById(inputId);
  var messages = document.getElementById(messagesId);
  if (!input || !messages || !input.value.trim()) return;

  var text = input.value.trim();
  input.value = '';
  if (input.style) input.style.height = 'auto';

  // User bubble
  messages.innerHTML += '<div class="panel-msg user-msg"><div class="panel-bubble">' + escapeHtml(text) + '</div></div>';

  // Typing indicator
  var typingId = 'typing-' + Date.now();
  messages.innerHTML += '<div class="panel-msg agent-msg" id="' + typingId + '"><div class="panel-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>';
  messages.scrollTop = messages.scrollHeight;

  try {
    var url  = API_BASE + '/agents/' + agentName + '/chat';
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 30000);
    var res  = await fetch(url, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: text }),
      signal:  ctrl.signal,
    });
    clearTimeout(timer);
    var data  = res.ok ? await res.json() : null;
    var reply = (data && (data.response || data.message)) ? (data.response || data.message)
              : res.ok ? JSON.stringify(data) : ('⚠️ Server error ' + res.status);

    var typing = document.getElementById(typingId);
    if (typing) {
      typing.querySelector('.panel-bubble').innerHTML = formatReply(reply);
      messages.scrollTop = messages.scrollHeight;
    }
  } catch (err) {
    var typing2 = document.getElementById(typingId);
    if (typing2) {
      typing2.querySelector('.panel-bubble').textContent =
        err.name === 'AbortError'
          ? '⏱️ Request timed out. The agent may be busy.'
          : '⚠️ Could not reach backend: ' + err.message;
      messages.scrollTop = messages.scrollHeight;
    }
  }
}

// Default sendPanelMsg — pages set window.AGENT_NAME
function sendPanelMsg() {
  var name = window.AGENT_NAME || 'nexus';
  universalSendMsg(name, 'panelInput', 'panelMessages');
}

// Enter to send, Shift+Enter for newline
document.addEventListener('DOMContentLoaded', function () {
  var input = document.getElementById('panelInput');
  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendPanelMsg();
      }
    });
    input.addEventListener('input', function () {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
  }
});

// ── HELPERS ──────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function formatReply(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
}
