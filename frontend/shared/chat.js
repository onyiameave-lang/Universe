/**
 * chat.js — Universe Ecosystem v5
 * AgentChat: Full-page ChatGPT-style chat for agent pages.
 *   - Left sidebar: session list + New Chat button
 *   - Main area: messages + composer
 *   - Sends to POST /agents/{id}/chat  body:{message:"..."}
 *   - Shows clear offline error when backend unreachable
 *   - localStorage session persistence per agent
 *
 * HeroChatManager: index.html hero→conversation transition.
 */

// ── OFFLINE MESSAGE ───────────────────────────────────────────────────────────
function _offlineMsg(agentId) {
  return `⚠️ **Backend offline** — ${agentId} is not responding.\n\nTo connect:\n1. Open a terminal in your project folder\n2. Run: \`python api.py\`\n3. Wait for "Uvicorn running on http://0.0.0.0:8000"\n4. Refresh this page and try again`;
}

// ── FORMAT TEXT ───────────────────────────────────────────────────────────────
function _fmt(text) {
  if (!text) return '';
  return text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\n/g,'<br>');
}

// ── SESSION STORE ─────────────────────────────────────────────────────────────
const SessionStore = {
  _key(agentId) { return `ue5-sessions-${agentId}`; },

  getAll(agentId) {
    try { return JSON.parse(localStorage.getItem(this._key(agentId)) || '[]'); }
    catch(_) { return []; }
  },

  save(agentId, sessions) {
    try { localStorage.setItem(this._key(agentId), JSON.stringify(sessions.slice(-50))); }
    catch(_) {}
  },

  newSession(agentId) {
    const id = `s_${Date.now()}`;
    const session = { id, title: 'New Chat', created: Date.now(), messages: [] };
    const all = this.getAll(agentId);
    all.unshift(session);
    this.save(agentId, all);
    return session;
  },

  getSession(agentId, sessionId) {
    return this.getAll(agentId).find(s => s.id === sessionId) || null;
  },

  updateSession(agentId, sessionId, messages) {
    const all = this.getAll(agentId);
    const idx = all.findIndex(s => s.id === sessionId);
    if (idx === -1) return;
    all[idx].messages = messages.slice(-100);
    // Auto-title from first user message
    if (all[idx].title === 'New Chat' && messages.length > 0) {
      const first = messages.find(m => m.role === 'user');
      if (first) all[idx].title = first.text.slice(0, 40) + (first.text.length > 40 ? '…' : '');
    }
    this.save(agentId, all);
  },

  deleteSession(agentId, sessionId) {
    const all = this.getAll(agentId).filter(s => s.id !== sessionId);
    this.save(agentId, all);
  },
};

// ── AGENT CHAT (agent pages) ──────────────────────────────────────────────────
/**
 * Full-page chat component for agent pages.
 *
 * HTML contract (agent page must have):
 *   #chatSessionList   — <ul> in the left sidebar for session items
 *   #chatNewBtn        — "New Chat" button
 *   #chatMessages      — messages container
 *   #chatInput         — textarea
 *   #chatSend          — send button
 *   #chatAgentName     — span showing agent name in header (optional)
 *   #chatEmptyState    — shown when no messages (optional)
 */
class AgentChat {
  constructor(agentId, opts = {}) {
    this.agentId     = agentId;
    this.msgsId      = opts.messagesId   || 'chatMessages';
    this.inputId     = opts.inputId      || 'chatInput';
    this.sendId      = opts.sendId       || 'chatSend';
    this.newBtnId    = opts.newBtnId     || 'chatNewBtn';
    this.listId      = opts.listId       || 'chatSessionList';
    this.emptyId     = opts.emptyId      || 'chatEmptyState';
    this._sending    = false;
    this._sessionId  = null;
    this._messages   = [];
    this._init();
  }

  _init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => this._setup());
    } else {
      this._setup();
    }
  }

  _setup() {
    this._bindEvents();
    this._loadOrCreateSession();
    this._renderSessionList();
  }

  _bindEvents() {
    const send  = document.getElementById(this.sendId);
    const input = document.getElementById(this.inputId);
    const newBtn = document.getElementById(this.newBtnId);

    if (send)  send.addEventListener('click', () => this._send());
    if (newBtn) newBtn.addEventListener('click', () => this.newChat());
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
      });
      input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 160) + 'px';
      });
    }
  }

  _loadOrCreateSession() {
    const all = SessionStore.getAll(this.agentId);
    if (all.length > 0) {
      const s = all[0];
      this._sessionId = s.id;
      this._messages  = s.messages || [];
      this._renderMessages();
    } else {
      const s = SessionStore.newSession(this.agentId);
      this._sessionId = s.id;
      this._messages  = [];
    }
    this._updateEmptyState();
  }

  _renderMessages() {
    const msgs = document.getElementById(this.msgsId);
    if (!msgs) return;
    msgs.innerHTML = '';
    this._messages.forEach(m => this._appendBubble(m.role, m.text, m.time, false));
    this._scrollToBottom();
  }

  _renderSessionList() {
    const list = document.getElementById(this.listId);
    if (!list) return;
    const all = SessionStore.getAll(this.agentId);
    if (!all.length) { list.innerHTML = '<li class="session-empty">No chats yet</li>'; return; }
    list.innerHTML = all.map(s => `
      <li class="session-item ${s.id === this._sessionId ? 'active' : ''}" data-id="${s.id}">
        <span class="session-title">${_fmt(s.title)}</span>
        <button class="session-del" data-id="${s.id}" title="Delete">✕</button>
      </li>`).join('');

    list.querySelectorAll('.session-item').forEach(li => {
      li.addEventListener('click', (e) => {
        if (e.target.classList.contains('session-del')) return;
        this._switchSession(li.dataset.id);
      });
    });
    list.querySelectorAll('.session-del').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._deleteSession(btn.dataset.id);
      });
    });
  }

  _switchSession(sessionId) {
    const s = SessionStore.getSession(this.agentId, sessionId);
    if (!s) return;
    this._sessionId = s.id;
    this._messages  = s.messages || [];
    this._renderMessages();
    this._renderSessionList();
    this._updateEmptyState();
  }

  _deleteSession(sessionId) {
    SessionStore.deleteSession(this.agentId, sessionId);
    if (this._sessionId === sessionId) {
      const all = SessionStore.getAll(this.agentId);
      if (all.length > 0) {
        this._switchSession(all[0].id);
      } else {
        this.newChat();
      }
    } else {
      this._renderSessionList();
    }
  }

  newChat() {
    const s = SessionStore.newSession(this.agentId);
    this._sessionId = s.id;
    this._messages  = [];
    const msgs = document.getElementById(this.msgsId);
    if (msgs) msgs.innerHTML = '';
    this._renderSessionList();
    this._updateEmptyState();
    const input = document.getElementById(this.inputId);
    if (input) { input.value = ''; input.style.height = 'auto'; input.focus(); }
  }

  _updateEmptyState() {
    const empty = document.getElementById(this.emptyId);
    if (!empty) return;
    empty.style.display = this._messages.length === 0 ? 'flex' : 'none';
  }

  async _send() {
    if (this._sending) return;
    const input = document.getElementById(this.inputId);
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    input.style.height = 'auto';
    this._sending = true;
    const sendBtn = document.getElementById(this.sendId);
    if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '…'; }

    // Hide empty state
    const empty = document.getElementById(this.emptyId);
    if (empty) empty.style.display = 'none';

    const time = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    this._appendBubble('user', text, time);
    this._messages.push({ role:'user', text, time });

    const typingId = `typing-${Date.now()}`;
    this._appendTyping(typingId);
    this._scrollToBottom();

    const reply = await chatAgent(this.agentId, text);

    const typingEl = document.getElementById(typingId);
    if (typingEl) typingEl.remove();

    const replyTime = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    const replyText = reply !== null ? reply : _offlineMsg(this.agentId);
    this._appendBubble('agent', replyText, replyTime);
    this._messages.push({ role:'agent', text: replyText, time: replyTime });

    SessionStore.updateSession(this.agentId, this._sessionId, this._messages);
    this._renderSessionList();
    this._scrollToBottom();

    this._sending = false;
    if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = '➤'; }
    if (input) input.focus();
  }

  _appendBubble(role, text, time, scroll = true) {
    const msgs = document.getElementById(this.msgsId);
    if (!msgs) return;
    const isUser = role === 'user';
    const agentInfo = typeof AGENTS !== 'undefined' ? AGENTS[this.agentId] : null;
    const agentColor = agentInfo ? agentInfo.color : '#6366F1';
    const agentName  = agentInfo ? agentInfo.name  : this.agentId;

    const div = document.createElement('div');
    div.className = `chat-msg ${isUser ? 'user' : 'agent'}`;
    if (isUser) {
      div.innerHTML = `
        <div class="chat-bubble user-bubble">${_fmt(text)}</div>
        <div class="chat-msg-time">${time}</div>`;
    } else {
      div.innerHTML = `
        <div class="chat-agent-label" style="color:${agentColor}">${agentName}</div>
        <div class="chat-bubble agent-bubble">${_fmt(text)}</div>
        <div class="chat-msg-time">${time}</div>`;
    }
    msgs.appendChild(div);
    if (scroll) this._scrollToBottom();
  }

  _appendTyping(id) {
    const msgs = document.getElementById(this.msgsId);
    if (!msgs) return;
    const agentInfo = typeof AGENTS !== 'undefined' ? AGENTS[this.agentId] : null;
    const agentColor = agentInfo ? agentInfo.color : '#6366F1';
    const div = document.createElement('div');
    div.className = 'chat-msg agent';
    div.id = id;
    div.innerHTML = `
      <div class="chat-bubble agent-bubble">
        <div class="chat-typing">
          <div class="typing-dot" style="background:${agentColor}"></div>
          <div class="typing-dot" style="background:${agentColor}"></div>
          <div class="typing-dot" style="background:${agentColor}"></div>
        </div>
      </div>`;
    msgs.appendChild(div);
  }

  _scrollToBottom() {
    const msgs = document.getElementById(this.msgsId);
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }
}

// ── HERO CHAT MANAGER (index.html) ────────────────────────────────────────────
/**
 * ChatGPT-style hero → conversation transition for index.html.
 *
 * HTML contract:
 *   #heroSection   — hero area (fades out on first send)
 *   #heroInput     — textarea in hero
 *   #heroSend      — send button in hero
 *   #convSection   — conversation view (hidden initially)
 *   #convMessages  — messages list
 *   #convInput     — textarea in conversation
 *   #convSend      — send button in conversation
 */
class HeroChatManager {
  constructor(opts = {}) {
    this.agentId     = opts.agentId     || 'nexus';
    this.heroId      = opts.heroId      || 'heroSection';
    this.convId      = opts.convId      || 'convSection';
    this.msgsId      = opts.msgsId      || 'convMessages';
    this.heroInputId = opts.heroInputId || 'heroInput';
    this.heroSendId  = opts.heroSendId  || 'heroSend';
    this.convInputId = opts.convInputId || 'convInput';
    this.convSendId  = opts.convSendId  || 'convSend';
    this.sessionKey  = `ue5-hero-session`;
    this._sending    = false;
    this._history    = [];
    this._inConv     = false;
    this._init();
  }

  _init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => this._setup());
    } else {
      this._setup();
    }
  }

  _setup() {
    this._bindHeroEvents();
    this._bindConvEvents();
    this._restoreSession();
  }

  _bindHeroEvents() {
    const heroInput = document.getElementById(this.heroInputId);
    const heroSend  = document.getElementById(this.heroSendId);
    if (heroSend)  heroSend.addEventListener('click', () => this._sendFromHero());
    if (heroInput) {
      heroInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._sendFromHero(); }
      });
      heroInput.addEventListener('input', () => {
        heroInput.style.height = 'auto';
        heroInput.style.height = Math.min(heroInput.scrollHeight, 120) + 'px';
      });
    }
  }

  _bindConvEvents() {
    const convInput = document.getElementById(this.convInputId);
    const convSend  = document.getElementById(this.convSendId);
    if (convSend)  convSend.addEventListener('click', () => this._sendFromConv());
    if (convInput) {
      convInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._sendFromConv(); }
      });
      convInput.addEventListener('input', () => {
        convInput.style.height = 'auto';
        convInput.style.height = Math.min(convInput.scrollHeight, 120) + 'px';
      });
    }
  }

  _restoreSession() {
    try {
      const saved = JSON.parse(localStorage.getItem(this.sessionKey) || '[]');
      if (Array.isArray(saved) && saved.length) {
        this._history = saved;
        this._transitionToConversation();
        saved.forEach(msg => this._appendBubble(msg.role, msg.text, msg.time, false));
        this._scrollToBottom();
      }
    } catch (_) {}
  }

  _saveSession() {
    try { localStorage.setItem(this.sessionKey, JSON.stringify(this._history.slice(-100))); }
    catch (_) {}
  }

  clearSession() {
    this._history = [];
    localStorage.removeItem(this.sessionKey);
    this._transitionToHero();
    const msgs = document.getElementById(this.msgsId);
    if (msgs) msgs.innerHTML = '';
  }

  _setPrompt(text) {
    const inp = document.getElementById(this._inConv ? this.convInputId : this.heroInputId);
    if (inp) { inp.value = text; inp.focus(); }
  }

  async _sendFromHero() {
    const input = document.getElementById(this.heroInputId);
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';
    this._transitionToConversation();
    await this._doSend(text);
  }

  async _sendFromConv() {
    const input = document.getElementById(this.convInputId);
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';
    await this._doSend(text);
  }

  async _doSend(text) {
    if (this._sending) return;
    this._sending = true;

    [this.heroSendId, this.convSendId].forEach(id => {
      const btn = document.getElementById(id);
      if (btn) { btn.disabled = true; btn.textContent = '…'; }
    });

    const time = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    this._appendBubble('user', text, time);
    this._history.push({ role:'user', text, time });

    const typingId = `typing-${Date.now()}`;
    this._appendTyping(typingId);
    this._scrollToBottom();

    const reply = await chatAgent(this.agentId, text);

    const typingEl = document.getElementById(typingId);
    if (typingEl) typingEl.remove();

    const replyTime = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    const replyText = reply !== null ? reply : _offlineMsg(this.agentId);
    this._appendBubble('agent', replyText, replyTime);
    this._history.push({ role:'agent', text: replyText, time: replyTime });
    this._saveSession();
    this._scrollToBottom();

    this._sending = false;
    [this.heroSendId, this.convSendId].forEach(id => {
      const btn = document.getElementById(id);
      if (btn) { btn.disabled = false; btn.textContent = '➤'; }
    });

    const convInput = document.getElementById(this.convInputId);
    if (convInput) convInput.focus();
  }

  _transitionToConversation() {
    if (this._inConv) return;
    this._inConv = true;
    const hero = document.getElementById(this.heroId);
    const conv = document.getElementById(this.convId);
    if (hero) {
      hero.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
      hero.style.opacity = '0';
      hero.style.transform = 'translateY(-20px)';
      setTimeout(() => { hero.style.display = 'none'; }, 300);
    }
    if (conv) {
      conv.style.display = 'flex';
      requestAnimationFrame(() => requestAnimationFrame(() => {
        conv.style.opacity = '1';
        conv.style.transform = 'translateY(0)';
      }));
    }
  }

  _transitionToHero() {
    this._inConv = false;
    const hero = document.getElementById(this.heroId);
    const conv = document.getElementById(this.convId);
    if (conv) {
      conv.style.opacity = '0';
      setTimeout(() => { conv.style.display = 'none'; }, 300);
    }
    if (hero) {
      hero.style.display = '';
      hero.style.opacity = '1';
      hero.style.transform = '';
    }
  }

  _appendBubble(role, text, time, scroll = true) {
    const msgs = document.getElementById(this.msgsId);
    if (!msgs) return;
    const isUser = role === 'user';
    const agentInfo = typeof AGENTS !== 'undefined' ? AGENTS[this.agentId] : null;
    const agentColor = agentInfo ? agentInfo.color : '#6366F1';
    const agentName  = agentInfo ? agentInfo.name  : 'Nexus';

    const div = document.createElement('div');
    div.className = `conv-msg ${isUser ? 'user' : 'agent'}`;
    if (isUser) {
      div.innerHTML = `
        <div class="conv-bubble user-bubble">${_fmt(text)}</div>
        <div class="conv-msg-time">${time}</div>`;
    } else {
      div.innerHTML = `
        <div class="conv-agent-label" style="color:${agentColor}">${agentName}</div>
        <div class="conv-bubble agent-bubble">${_fmt(text)}</div>
        <div class="conv-msg-time">${time}</div>`;
    }
    msgs.appendChild(div);
    if (scroll) this._scrollToBottom();
  }

  _appendTyping(id) {
    const msgs = document.getElementById(this.msgsId);
    if (!msgs) return;
    const agentInfo = typeof AGENTS !== 'undefined' ? AGENTS[this.agentId] : null;
    const agentColor = agentInfo ? agentInfo.color : '#6366F1';
    const div = document.createElement('div');
    div.className = 'conv-msg agent';
    div.id = id;
    div.innerHTML = `
      <div class="conv-bubble agent-bubble">
        <div class="chat-typing">
          <div class="typing-dot" style="background:${agentColor}"></div>
          <div class="typing-dot" style="background:${agentColor}"></div>
          <div class="typing-dot" style="background:${agentColor}"></div>
        </div>
      </div>`;
    msgs.appendChild(div);
  }

  _scrollToBottom() {
    const msgs = document.getElementById(this.msgsId);
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
    const conv = document.getElementById(this.convId);
    if (conv) conv.scrollTop = conv.scrollHeight;
  }
}

// ── PAGE TRANSITIONS ──────────────────────────────────────────────────────────
(function initPageTransitions() {
  const setup = () => {
    document.body.classList.add('page-ready');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      document.body.classList.add('page-visible');
    }));
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a[href]');
      if (!link) return;
      const href = link.getAttribute('href');
      if (!href || href.startsWith('http') || href.startsWith('#') || href.startsWith('mailto')) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      document.body.classList.remove('page-visible');
      document.body.classList.add('page-leaving');
      setTimeout(() => { window.location.href = href; }, 220);
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
})();

// ── KPI COUNTER ANIMATION ─────────────────────────────────────────────────────
const _animatedKPIs = new WeakSet();
function animateKPIs() {
  document.querySelectorAll('[data-target]:not([data-animated])').forEach(el => {
    if (_animatedKPIs.has(el)) return;
    const run = () => {
      if (_animatedKPIs.has(el)) return;
      _animatedKPIs.add(el);
      el.dataset.animated = '1';
      const target   = parseFloat(el.dataset.target);
      const prefix   = el.dataset.prefix  || '';
      const suffix   = el.dataset.suffix  || '';
      const decimals = parseInt(el.dataset.decimals || '0');
      const start    = performance.now();
      const step = (now) => {
        const p = Math.min((now - start) / 1200, 1);
        const ease = 1 - Math.pow(1 - p, 3);
        el.textContent = prefix + (target * ease).toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ',') + suffix;
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };
    if ('IntersectionObserver' in window) {
      const obs = new IntersectionObserver(entries => {
        entries.forEach(e => { if (e.isIntersecting) { run(); obs.unobserve(e.target); } });
      }, { threshold: 0.1 });
      obs.observe(el);
    } else { run(); }
  });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', animateKPIs);
else animateKPIs();

// ── CONFIDENCE BAR ANIMATION ──────────────────────────────────────────────────
const _animatedBars = new WeakSet();
function animateConfBars() {
  document.querySelectorAll('.conf-bar[data-width]').forEach(bar => {
    if (_animatedBars.has(bar)) return;
    const run = () => {
      if (_animatedBars.has(bar)) return;
      _animatedBars.add(bar);
      bar.style.transition = 'none';
      bar.style.width = '0%';
      requestAnimationFrame(() => requestAnimationFrame(() => {
        bar.style.transition = 'width 0.9s cubic-bezier(0.4,0,0.2,1)';
        bar.style.width = parseFloat(bar.dataset.width) + '%';
      }));
    };
    if ('IntersectionObserver' in window) {
      const obs = new IntersectionObserver(entries => {
        entries.forEach(e => { if (e.isIntersecting) { run(); obs.unobserve(e.target); } });
      }, { threshold: 0.05 });
      obs.observe(bar);
    } else { run(); }
  });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', animateConfBars);
else animateConfBars();
