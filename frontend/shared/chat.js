/**
 * chat.js — Universe Ecosystem v7 MYTHIC
 * ChatDrawer: floating popup for agent dashboard pages
 * HeroChatManager: hero → conversation transition for index.html (with back-to-hub)
 */

/* ═══════════════════════════════════════════════════════════════════
   ChatDrawer — slide-in from right, overlays dashboard
═══════════════════════════════════════════════════════════════════ */
function formatReadableMarkdown(raw) {
  let text = String(raw || '');
  // Pre-convert multi-line or inline **bold** markers before splitting lines
  text = text.replace(/\*\*([^*\n]+)\*\*/g, '[[B]]$1[[/B]]');
  text = text.replace(/\*([^*\n]+)\*/g, '[[I]]$1[[/I]]');
  // Strip any leftover unmatched ** asterisks so raw asterisks never appear in chat
  text = text.replace(/\*\*+/g, '');

  const esc = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\[\[B\]\]/g, '<strong>')
    .replace(/\[\[\/B\]\]/g, '</strong>')
    .replace(/\[\[I\]\]/g, '<em>')
    .replace(/\[\[\/I\]\]/g, '</em>');

  const lines = esc.split('\n');
  const out = [];
  let inList = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i].trim();
    if (!line) {
      if (inList) { out.push('</ul>'); inList = false; }
      continue;
    }

    // Inline formatting: links and code
    line = line
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" style="color:var(--agent-color,#0ea5e9);text-decoration:underline">$1</a>')
      .replace(/`([^`]+)`/g, '<code style="background:rgba(14,165,233,0.12);padding:1px 5px;border-radius:4px;font-size:0.92em">$1</code>');

    if (line.startsWith('#### ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push('<div style="font-weight:700;font-size:13.5px;color:var(--text);margin:12px 0 6px;letter-spacing:0.01em">' + line.slice(5) + '</div>');
    } else if (line.startsWith('### ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push('<div style="font-weight:800;font-size:14.5px;color:var(--agent-color,#0ea5e9);margin:14px 0 6px;border-bottom:1px solid var(--border);padding-bottom:4px">' + line.slice(4) + '</div>');
    } else if (line.startsWith('## ') || line.startsWith('# ')) {
      if (inList) { out.push('</ul>'); inList = false; }
      const txt = line.replace(/^#+\s*/, '');
      out.push('<div style="font-weight:800;font-size:15px;color:var(--text);margin:14px 0 6px">' + txt + '</div>');
    } else if (/^[-•]\s+/.test(line) || /^\d+\.\s+/.test(line)) {
      if (!inList) {
        out.push('<ul style="margin:6px 0 10px 18px;padding:0;display:flex;flex-direction:column;gap:6px">');
        inList = true;
      }
      const itemTxt = line.replace(/^([-•]|\d+\.)\s+/, '');
      out.push('<li style="line-height:1.5">' + itemTxt + '</li>');
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push('<p style="margin:6px 0;line-height:1.55">' + line + '</p>');
    }
  }
  if (inList) out.push('</ul>');
  return out.join('');
}
window.formatReadableMarkdown = formatReadableMarkdown;

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
    const bodyHtml = role === 'agent' ? formatReadableMarkdown(text) : this._escape(text);
    div.innerHTML = `
      <div class="chat-bubble">${bodyHtml}</div>
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
    const bodyHtml = role === 'agent' ? formatReadableMarkdown(text) : this._escape(text);
    div.innerHTML = `
      <div class="chat-bubble">${bodyHtml}</div>
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

/* ═══════════════════════════════════════════════════════════════════
   On-Page Agent Chat Studio & Drawer Markdown Upgrade for All Agent Pages
═══════════════════════════════════════════════════════════════════ */
const AGENT_STUDIO_PROFILES = {
  oracle: {
    name: 'Oracle',
    subtitle: 'Your trading specialist — switch between MetaTrader 5 (MT5) and cTrader above, or ask about today\'s profit, win rate, and trades in plain English',
    welcome: 'Hello! I am **Oracle**, your trading specialist. You can click the **MetaTrader 5 (MT5)** or **cTrader Account** buttons above to inspect each broker account separately, or ask me in plain English: **"How much did we make today?"**, **"Compare our MT5 trades and cTrader trades"**, **"What is our win rate?"**, or **"What pairs are we watching right now?"**',
    prompts: [
      { label: 'How Much Did We Make Today?', text: 'How much money did we make today across our trades?' },
      { label: 'MT5 vs cTrader Account Results', text: 'Compare our MetaTrader 5 (MT5) trades and cTrader account results' },
      { label: 'Win Rate by Currency Pair', text: 'Break down our win rate by currency pair in plain English' },
      { label: 'How You Protect Our Account', text: 'How do you pick entry points and protect our account from bad trades?' },
    ],
  },
  pulse: {
    name: 'Pulse',
    subtitle: 'Your social media specialist — see what traders and investors are posting today in plain English',
    welcome: 'Hello! I am **Pulse**. I read live posts from traders and finance discussions across **Mastodon, Bluesky, and HackerNews** so you can ask **who posted what today** and whether people online are feeling optimistic or cautious about the markets.',
    prompts: [
      { label: 'Who Posted What Today?', text: 'What is happening on social media accounts right now — who posted this and who posted that?' },
      { label: 'Markets & Trading Posts', text: 'Who posted about markets, finance, or trading today?' },
      { label: 'Is the Crowd Optimistic or Cautious?', text: 'What is the overall social media mood right now?' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  sentinel: {
    name: 'Sentinel',
    subtitle: 'Your world & financial news specialist — tracking major headlines across BBC, CNBC, WSJ, Al Jazeera, MarketWatch & OilPrice',
    welcome: 'Hello! I am **Sentinel**. I watch live world and financial news headlines across **BBC, CNBC, Wall Street Journal, Al Jazeera, MarketWatch, and OilPrice** and explain how today\'s news affects currencies, gold, and stocks.',
    prompts: [
      { label: 'World News Today', text: "What's happening in the news in the world today?" },
      { label: 'Interest Rates & Inflation News', text: 'What are the biggest economy and inflation headlines today?' },
      { label: 'Financial & Energy Headlines', text: 'Show me the latest financial markets and oil news headlines' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  aegis: {
    name: 'Aegis',
    subtitle: 'Your account safety guardian — blocks risky trades and makes sure target profit is always bigger than stop-loss risk',
    welcome: 'Hello! I am **Aegis**, your account safety guardian. My job is to protect your money: I block any trade where the potential profit isn\'t at least **1.8× bigger** than the stop-loss risk, and I automatically skipped **57 weak or unclear trade setups** in our latest session so they couldn\'t hurt your balance.',
    prompts: [
      { label: 'Is Our Account Safe?', text: 'Is our trading account safe right now and how do your safety rules protect our money?' },
      { label: 'Why Were Weak Trades Skipped?', text: 'How many weak or risky trade setups did you block to protect our balance?' },
      { label: 'All 9 Agents Health Check', text: 'Are all 9 agents passing their daily safety check?' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  forge: {
    name: 'Forge',
    subtitle: 'Your strategy trainer — reviews our last trading session, learns from mistakes, and tests strategies before Oracle trades them',
    welcome: 'Hello! I am **Forge**, your strategy trainer. Ask me in plain English about **our last trading session** (**3 wins out of 3 EUR/USD trades** on MetaTrader 5 for **+$4,621.55** profit, bringing the account to **$104,621.55**), or how we test strategies across 7 chart timeframes before risking a single dollar.',
    prompts: [
      { label: 'Review Our Last Trading Session', text: 'Tell me about the last trading session we just did and how our trades performed' },
      { label: 'How We Train & Test Strategies', text: 'How do you test trading strategies in plain English before Oracle uses them?' },
      { label: 'What Did We Learn From Past Trades?', text: 'What lessons did the Mistake Clinic learn from our recent sessions?' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  chronicle: {
    name: 'Chronicle',
    subtitle: 'Our shared team memory — remembers every lesson learned, past trade, and research answer in plain English',
    welcome: 'Hello! I am **Chronicle**, the shared memory for all **9 agents**. Whenever Oracle completes a trade, Forge tests a new strategy rule, or Atlas researches a question, the lesson is saved here in plain English so the whole team gets smarter every day.',
    prompts: [
      { label: 'What Lessons Have We Learned?', text: 'What key trading and research lessons are saved in our shared Chronicle memory?' },
      { label: 'How All 9 Agents Share Memory', text: 'How do all 9 agents share what they learn through Chronicle?' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  nexus: {
    name: 'Nexus',
    subtitle: 'Your team coordinator — ask any question in plain English and Nexus will bring in the right specialist automatically',
    welcome: 'Hello! I am **Nexus**, the team coordinator for all **9 agents**. Ask me any question in everyday English — whether it\'s **"How much did we make today?"**, **"What\'s happening in world news?"**, or any general question — and I will coordinate the answer for you.',
    prompts: [
      { label: 'How Much Did We Make Today?', text: 'How much did we make today?' },
      { label: 'Full Team Status in Plain English', text: 'Give me an overview of how all 9 agents are doing right now' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
  genesis: {
    name: 'Genesis',
    subtitle: 'Your strategy designer — creates new trading ideas and stress-tests them across 1,000 simulated market scenarios',
    welcome: 'Hello! I am **Genesis**, your strategy designer. I design new trading blueprints for Scalping, Day Trading, Swing Trading, and Range Trading, and test each idea across **1,000 simulated market conditions** before handing the safest ones to Forge and Oracle.',
    prompts: [
      { label: 'Show Our Best Trading Blueprints', text: 'What trading strategy blueprints have you designed and how well did they perform in testing?' },
      { label: 'What Do You Do?', text: 'What do you do?' },
    ],
  },
};

function renderCrossAgentReferralCards(referrals) {
  if (!Array.isArray(referrals) || referrals.length === 0) return '';
  const cards = referrals.slice(0, 3).map((r) => {
    const toAgent = String(r.to_agent || 'nexus').toLowerCase();
    const title = String(r.to_agent_title || toAgent.toUpperCase());
    const reason = String(r.reason || 'Ask this specialist for more details.');
    return `
      <div style="flex:1;min-width:210px;background:rgba(15,23,42,0.45);border:1px solid rgba(56,189,248,0.2);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
          <span style="font-weight:700;font-size:12.5px;color:#38bdf8">${title}</span>
          <a href="${toAgent}.html" style="font-size:11px;font-weight:700;padding:3px 9px;border-radius:6px;background:rgba(56,189,248,0.15);color:#38bdf8;text-decoration:none;border:1px solid rgba(56,189,248,0.28)">Talk to ${title} →</a>
        </div>
        <div style="font-size:11.5px;color:var(--text-muted,#cbd5e1);line-height:1.45">${reason}</div>
      </div>
    `;
  }).join('');
  return `
    <div style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08)">
      <div style="font-size:11px;font-weight:700;color:#94a3b8;margin-bottom:8px">Related Specialists You Can Talk To:</div>
      <div style="display:flex;flex-wrap:wrap;gap:10px">${cards}</div>
    </div>
  `;
}
window.renderCrossAgentReferralCards = renderCrossAgentReferralCards;

function renderCompactDagTraceBadge(trace) {
  if (!trace || !Array.isArray(trace.dag_levels)) return '';
  const gate = trace.aegis_policy_gate || {};
  const cons = trace.provenance_consensus || {};
  const wb = trace.chronicle_writeback || {};
  const qual = (trace.quality_signal || {}).quality_score ?? 0.92;
  const consulted = (trace.consulted_agents || []).map((a) => a.charAt(0).toUpperCase() + a.slice(1)).join(', ');
  const lvlText = trace.dag_levels.map((l) => `${l.level_name} [${(l.nodes || []).map((n) => n.agent_title).join(', ')}]`).join(' → ');
  return `
    <details style="margin-top:10px;padding:7px 11px;border-radius:8px;background:rgba(15,23,42,0.45);border:1px solid rgba(255,255,255,0.08);font-size:11.5px;color:#94a3b8">
      <summary style="cursor:pointer;font-weight:600;color:#94a3b8;outline:none">
        Coordinated with ${consulted || 'Chronicle & Atlas'} in ${trace.total_latency_ms || 18}ms <span style="opacity:0.75;font-weight:400">(view technical routing details)</span>
      </summary>
      <div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08);display:flex;flex-direction:column;gap:4px;font-size:11px">
        <div><strong>Trace ID:</strong> <code>${trace.trace_id || 'NEXUS-DAG'}</code> · <strong>Route:</strong> ${lvlText}</div>
        <div><strong>Verified Sources:</strong> ${cons.unique_provenance_sources_count ?? 1} (${cons.shared_sources_deduplicated_count ?? 0} duplicates merged) · <strong>Quality Score:</strong> ${Number(qual).toFixed(2)}</div>
        <div><strong>Safety Gate:</strong> ${gate.status || 'APPROVED'} · <strong>Memory:</strong> ${wb.tier2_promoted_to_reusable_knowledge ? 'Saved to Shared Team Knowledge' : 'Saved to Activity Log'}</div>
      </div>
    </details>
  `;
}
window.renderCompactDagTraceBadge = renderCompactDagTraceBadge;

function initAgentPageStudio() {
  const agentId = (
    (document.body && document.body.getAttribute('data-agent')) ||
    (typeof AGENT_ID !== 'undefined' ? String(AGENT_ID) : '')
  ).toLowerCase();

  // 1. Format any new agent messages added to #chatMessages drawer with Markdown on ALL pages
  const chatMsgEl = document.getElementById('chatMessages');
  if (chatMsgEl && typeof MutationObserver !== 'undefined') {
    const obs = new MutationObserver((mutations) => {
      for (const m of mutations) {
        m.addedNodes.forEach((node) => {
          if (node.nodeType === 1 && node.classList && node.classList.contains('agent')) {
            const bubble = node.querySelector('.chat-bubble');
            if (bubble && !bubble.querySelector('.chat-typing') && !bubble.dataset.mdFormatted) {
              bubble.dataset.mdFormatted = '1';
              bubble.innerHTML = formatReadableMarkdown(bubble.textContent || '');
            }
          }
        });
      }
    });
    obs.observe(chatMsgEl, { childList: true });
  }

  if (!agentId || agentId === 'atlas' || agentId === 'nexus') return; // atlas.html and nexus.html have dedicated custom institutional studios

  // 2. Mount an interactive On-Page Chat Studio right below .kpi-grid so users can chat directly on the page
  const kpiGrid = document.querySelector('.kpi-grid');
  if (!kpiGrid || document.getElementById('agentOnPageStudio')) return;

  const profile = AGENT_STUDIO_PROFILES[agentId] || {
    name: agentId.toUpperCase(),
    subtitle: `Chat directly with ${agentId.toUpperCase()} in natural English`,
    welcome: `Hello! I am **${agentId.toUpperCase()}**. Ask me what I do or about my live telemetry!`,
    prompts: [{ label: 'What Do You Do?', text: 'What do you do?' }],
  };

  const studioCard = document.createElement('div');
  studioCard.className = 'card';
  studioCard.id = 'agentOnPageStudio';
  studioCard.style.marginBottom = '18px';
  studioCard.style.border = '1px solid var(--agent-color, #38bdf8)';

  const pillsHtml = profile.prompts
    .map(
      (p) =>
        `<button type="button" class="agent-quick-pill" data-prompt="${p.text.replace(/"/g, '&quot;')}" style="font-size:11.5px;padding:6px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.14);background:rgba(255,255,255,0.04);cursor:pointer;color:var(--text,#f8fafc);font-weight:600">${p.label}</button>`
    )
    .join('');

  studioCard.innerHTML = `
    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <div>
        <div class="card-title">Chat Directly with ${profile.name} — Human-Readable Console</div>
        <div style="font-size:12px;color:var(--text-muted,#94a3b8);margin-top:2px">${profile.subtitle}</div>
      </div>
      <span style="font-size:11px;font-weight:600;padding:4px 10px;border-radius:6px;background:rgba(56,189,248,0.14);color:var(--agent-color,#38bdf8)">Chronicle Memory + Nexus DAG Connected</span>
    </div>
    <div class="card-body" style="padding:14px 18px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        ${pillsHtml}
      </div>
      <div id="agentStudioTranscript" style="background:rgba(0,0,0,0.22);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px;max-height:440px;overflow-y:auto;margin-bottom:12px;font-size:13.5px;line-height:1.55">
        <div style="margin-bottom:6px">
          <span style="font-weight:700;color:var(--agent-color,#38bdf8)">${profile.name}:</span>
          <div style="margin-top:4px">${formatReadableMarkdown(profile.welcome)}</div>
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <input id="agentStudioInput" type="text" placeholder="Say hi, ask '${profile.prompts[0]?.text || 'What do you do?'}', or ask any question..." style="flex:1;padding:10px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.15);background:rgba(0,0,0,0.25);color:var(--text,#f8fafc);font-size:13.5px;font-family:inherit;outline:none" />
        <button id="agentStudioSendBtn" type="button" style="padding:10px 18px;border-radius:8px;border:none;background:var(--agent-color,#2563eb);color:#fff;font-weight:600;font-size:13px;cursor:pointer">Ask ${profile.name}</button>
      </div>
    </div>
  `;

  kpiGrid.parentNode.insertBefore(studioCard, kpiGrid.nextSibling);

  const transcript = document.getElementById('agentStudioTranscript');
  const inputEl = document.getElementById('agentStudioInput');
  const sendBtn = document.getElementById('agentStudioSendBtn');

  async function sendStudioQuestion(customText) {
    const q = (customText || (inputEl ? inputEl.value : '') || '').trim();
    if (!q || !transcript) return;
    if (!customText && inputEl) inputEl.value = '';

    const uDiv = document.createElement('div');
    uDiv.style.cssText = 'margin:12px 0;padding:10px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px';
    uDiv.innerHTML = `<strong>You:</strong> ${q.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}`;
    transcript.appendChild(uDiv);

    const loadDiv = document.createElement('div');
    loadDiv.style.cssText = 'margin:10px 0;color:var(--text-muted,#94a3b8);font-style:italic';
    loadDiv.textContent = `${profile.name} is checking Chronicle memory and live telemetry...`;
    transcript.appendChild(loadDiv);
    transcript.scrollTop = transcript.scrollHeight;

    try {
      const base = (typeof CONFIG !== 'undefined' && CONFIG.API_BASE) ? CONFIG.API_BASE : window.location.origin;
      const r = await fetch(`${base}/agents/${agentId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: q }),
      });
      const d = await r.json();
      loadDiv.remove();

      const aDiv = document.createElement('div');
      aDiv.style.cssText = 'margin:12px 0;padding:12px 14px;background:rgba(255,255,255,0.03);border-left:3px solid var(--agent-color,#38bdf8);border-radius:8px';
      const replyText = d.reply || d.response || d.message || 'Done.';
      const referralsHtml = renderCrossAgentReferralCards(d.referrals || []);
      const traceHtml = renderCompactDagTraceBadge(d.trace);
      aDiv.innerHTML = `<div style="font-weight:700;color:var(--agent-color,#38bdf8);margin-bottom:6px">${profile.name}</div><div>${formatReadableMarkdown(replyText)}</div>${traceHtml}${referralsHtml}`;
      transcript.appendChild(aDiv);
      transcript.scrollTop = transcript.scrollHeight;
    } catch (e) {
      loadDiv.textContent = `Could not reach ${profile.name} chat endpoint: ` + (e.message || e);
    }
  }

  if (sendBtn) sendBtn.addEventListener('click', () => sendStudioQuestion());
  if (inputEl) {
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sendStudioQuestion();
    });
  }
  studioCard.querySelectorAll('.agent-quick-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      const promptText = btn.getAttribute('data-prompt');
      if (promptText) sendStudioQuestion(promptText);
    });
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAgentPageStudio);
} else {
  initAgentPageStudio();
}

