/**
 * chat.js — Universe Ecosystem v7 MYTHIC
 * ChatDrawer: floating popup for agent dashboard pages
 * HeroChatManager: hero → conversation transition for index.html (with back-to-hub)
 */

/* ═══════════════════════════════════════════════════════════════════
   ChatDrawer — slide-in from right, overlays dashboard
═══════════════════════════════════════════════════════════════════ */
class ChatDrawer {
  constructor(agentId, opts = {}) {
    this.agentId = agentId;
    this.opts = {
      drawerSelector:   '#chatDrawer',
      overlaySelector:  '#chatOverlay',
      fabSelector:      '#chatFab',
      messagesSelector: '#chatDrawerMessages',
      emptySelector:    '#chatDrawerEmpty',
      inputSelector:    '#chatDrawerInput',
      sendSelector:     '#chatDrawerSend',
      closeSelector:    '#chatDrawerClose',
      ...opts,
    };
    this.messages = [];
    this.sending  = false;
    this._init();
  }

  _init() {
    this.drawer   = document.querySelector(this.opts.drawerSelector);
    this.overlay  = document.querySelector(this.opts.overlaySelector);
    this.fab      = document.querySelector(this.opts.fabSelector);
    this.msgWrap  = document.querySelector(this.opts.messagesSelector);
    this.empty    = document.querySelector(this.opts.emptySelector);
    this.input    = document.querySelector(this.opts.inputSelector);
    this.sendBtn  = document.querySelector(this.opts.sendSelector);
    this.closeBtn = document.querySelector(this.opts.closeSelector);

    if (!this.drawer) return;

    if (this.fab)      this.fab.addEventListener('click', () => this.open());
    if (this.overlay)  this.overlay.addEventListener('click', () => this.close());
    if (this.closeBtn) this.closeBtn.addEventListener('click', () => this.close());
    if (this.sendBtn)  this.sendBtn.addEventListener('click', () => this._send());

    if (this.input) {
      this.input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
      });
      this.input.addEventListener('input', () => this._autoGrow());
    }

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && this.isOpen()) this.close();
    });

    window._chatDrawer = this;
  }

  open() {
    if (!this.drawer) return;
    this.drawer.classList.add('open');
    if (this.overlay) this.overlay.classList.add('visible');
    if (this.input) setTimeout(() => this.input.focus(), 300);
  }

  close() {
    if (!this.drawer) return;
    this.drawer.classList.remove('open');
    if (this.overlay) this.overlay.classList.remove('visible');
  }

  isOpen() { return this.drawer && this.drawer.classList.contains('open'); }

  fillInput(text) {
    if (!this.input) return;
    this.input.value = text;
    this._autoGrow();
    this.open();
    this.input.focus();
  }

  _autoGrow() {
    if (!this.input) return;
    this.input.style.height = 'auto';
    this.input.style.height = Math.min(this.input.scrollHeight, 120) + 'px';
  }

  async _send() {
    if (!this.input || this.sending) return;
    const text = this.input.value.trim();
    if (!text) return;

    this.input.value = '';
    this._autoGrow();
    this._addMessage('user', text);
    this._showTyping();
    this.sending = true;
    if (this.sendBtn) this.sendBtn.disabled = true;

    const reply = await chatAgent(this.agentId, text);

    this._hideTyping();
    this.sending = false;
    if (this.sendBtn) this.sendBtn.disabled = false;

    this._addMessage('agent',
      reply === null
        ? 'The backend is still working or reconnecting. Please try again in a moment.'
        : reply
    );
  }

  _addMessage(role, text) {
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    this.messages.push({ role, text, time: now });
    if (this.empty) this.empty.style.display = 'none';

    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.innerHTML = `
      <div class="chat-bubble">${this._escape(text)}</div>
      <div class="chat-msg-time">${now}</div>`;
    if (this.msgWrap) {
      this.msgWrap.appendChild(div);
      this.msgWrap.scrollTop = this.msgWrap.scrollHeight;
    }
  }

  _showTyping() {
    if (!this.msgWrap) return;
    const div = document.createElement('div');
    div.className = 'chat-msg agent';
    div.id = '_typing';
    div.innerHTML = `<div class="chat-bubble chat-typing">
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
    </div>`;
    this.msgWrap.appendChild(div);
    this.msgWrap.scrollTop = this.msgWrap.scrollHeight;
  }

  _hideTyping() {
    const el = document.getElementById('_typing');
    if (el) el.remove();
  }

  _escape(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>');
  }
}

/* ═══════════════════════════════════════════════════════════════════
   HeroChatManager — index.html hero ↔ conversation
   NEW: back-to-hub button restores hero + agent cards
═══════════════════════════════════════════════════════════════════ */
class HeroChatManager {
  constructor(opts = {}) {
    this.agentId      = opts.agentId      || 'nexus';
    this.heroSection  = document.getElementById(opts.heroId      || 'heroSection');
    this.convSection  = document.getElementById(opts.convId      || 'convSection');
    this.heroInput    = document.getElementById(opts.heroInputId  || 'heroInput');
    this.heroSend     = document.getElementById(opts.heroSendId   || 'heroSend');
    this.convMessages = document.getElementById(opts.convMsgId    || 'convMessages');
    this.convInput    = document.getElementById(opts.convInputId  || 'convInput');
    this.convSend     = document.getElementById(opts.convSendId   || 'convSend');
    this.backBtn      = document.getElementById(opts.backBtnId    || 'backToHubBtn');
    this.sending      = false;
    this._init();
  }

  _init() {
    if (this.heroSend) this.heroSend.addEventListener('click', () => this._heroSend());
    if (this.heroInput) {
      this.heroInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._heroSend(); }
      });
      this.heroInput.addEventListener('input', () => this._autoGrow(this.heroInput));
    }

    if (this.convSend) this.convSend.addEventListener('click', () => this._convSend());
    if (this.convInput) {
      this.convInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._convSend(); }
      });
      this.convInput.addEventListener('input', () => this._autoGrow(this.convInput));
    }

    // Back-to-hub button
    if (this.backBtn) {
      this.backBtn.addEventListener('click', () => this._backToHub());
    }
  }

  _autoGrow(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  async _heroSend() {
    if (!this.heroInput || this.sending) return;
    const text = this.heroInput.value.trim();
    if (!text) return;
    this.heroInput.value = '';
    this._autoGrow(this.heroInput);
    this._transitionToConv();
    this._addMessage('user', text);
    this._showTyping();
    this.sending = true;
    const reply = await chatAgent(this.agentId, text);
    this._hideTyping();
    this.sending = false;
    this._addMessage('agent',
      reply ?? 'The backend is still working or reconnecting. Please try again in a moment.'
    );
  }

  async _convSend() {
    if (!this.convInput || this.sending) return;
    const text = this.convInput.value.trim();
    if (!text) return;
    this.convInput.value = '';
    this._autoGrow(this.convInput);
    this._addMessage('user', text);
    this._showTyping();
    this.sending = true;
    const reply = await chatAgent(this.agentId, text);
    this._hideTyping();
    this.sending = false;
    this._addMessage('agent',
      reply ?? 'The backend is still working or reconnecting. Please try again in a moment.'
    );
  }

  _transitionToConv() {
    if (this.heroSection) {
      this.heroSection.style.transition = 'opacity 0.35s ease, transform 0.35s ease';
      this.heroSection.style.opacity    = '0';
      this.heroSection.style.transform  = 'translateY(-20px)';
      setTimeout(() => { this.heroSection.style.display = 'none'; }, 350);
    }
    if (this.convSection) {
      this.convSection.style.display = 'flex';
      setTimeout(() => { this.convSection.style.opacity = '1'; }, 50);
    }
  }

  _backToHub() {
    // Clear conversation messages
    if (this.convMessages) this.convMessages.innerHTML = '';

    // Hide conversation
    if (this.convSection) {
      this.convSection.style.opacity = '0';
      setTimeout(() => { this.convSection.style.display = 'none'; }, 300);
    }

    // Restore hero
    if (this.heroSection) {
      this.heroSection.style.display = 'flex';
      this.heroSection.style.transform = 'translateY(0)';
      setTimeout(() => {
        this.heroSection.style.opacity = '1';
        if (this.heroInput) this.heroInput.focus();
      }, 50);
    }
  }

  _addMessage(role, text) {
    if (!this.convMessages) return;
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.innerHTML = `
      <div class="chat-bubble">${this._escape(text)}</div>
      <div class="chat-msg-time">${now}</div>`;
    this.convMessages.appendChild(div);
    this.convMessages.scrollTop = this.convMessages.scrollHeight;
  }

  _showTyping() {
    if (!this.convMessages) return;
    const div = document.createElement('div');
    div.className = 'chat-msg agent';
    div.id = '_heroTyping';
    div.innerHTML = `<div class="chat-bubble chat-typing">
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
    </div>`;
    this.convMessages.appendChild(div);
    this.convMessages.scrollTop = this.convMessages.scrollHeight;
  }

  _hideTyping() {
    const el = document.getElementById('_heroTyping');
    if (el) el.remove();
  }

  _escape(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>');
  }
}

/* ═══════════════════════════════════════════════════════════════════
   Legacy AgentChat stub (backward compat)
═══════════════════════════════════════════════════════════════════ */
class AgentChat {
  constructor(agentId, opts = {}) {
    if (document.querySelector('#chatDrawer')) {
      this._drawer = new ChatDrawer(agentId, {
        messagesSelector: opts.messagesId ? `#${opts.messagesId}` : '#chatDrawerMessages',
        inputSelector:    opts.inputId    ? `#${opts.inputId}`    : '#chatDrawerInput',
        sendSelector:     opts.sendId     ? `#${opts.sendId}`     : '#chatDrawerSend',
      });
    }
  }
  _fillInput(text) { if (this._drawer) this._drawer.fillInput(text); }
}
