/**
 * websocket.js — Universe Ecosystem v3
 * WebSocketManager with exponential backoff reconnection + polling fallback.
 */

class WebSocketManager {
  /**
   * @param {string} agentId
   * @param {Function} onMessage  called with parsed JSON data
   * @param {object}  [opts]
   * @param {number}  [opts.pollInterval=30000]  fallback polling interval (ms)
   * @param {number}  [opts.maxRetries=10]
   * @param {number}  [opts.baseDelay=1000]      initial reconnect delay (ms)
   * @param {number}  [opts.maxDelay=30000]      max reconnect delay (ms)
   */
  constructor(agentId, onMessage, opts = {}) {
    this.agentId      = agentId;
    this.onMessage    = onMessage;
    this.pollInterval = opts.pollInterval ?? 30000;
    this.maxRetries   = opts.maxRetries   ?? 10;
    this.baseDelay    = opts.baseDelay    ?? 1000;
    this.maxDelay     = opts.maxDelay     ?? 30000;

    this._ws          = null;
    this._retries     = 0;
    this._reconnTimer = null;
    this._pollTimer   = null;
    this._mode        = 'idle';   // 'ws' | 'poll' | 'idle'
    this._destroyed   = false;
  }

  /** Start — try WebSocket first, fall back to polling. */
  start() {
    this._destroyed = false;
    this._tryWebSocket();
  }

  /** Stop everything. */
  stop() {
    this._destroyed = true;
    this._clearTimers();
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
    this._mode = 'idle';
  }

  // ── WEBSOCKET ───────────────────────────────────────────────────────────────
  _tryWebSocket() {
    if (this._destroyed) return;
    const url = API.ws(this.agentId);
    try {
      this._ws = new WebSocket(url);
    } catch (_) {
      this._fallbackToPoll();
      return;
    }

    this._ws.onopen = () => {
      this._retries = 0;
      this._mode = 'ws';
      this._clearPoll();
      console.debug(`[WS:${this.agentId}] connected`);
    };

    this._ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this.onMessage(data);
      } catch (_) {
        this.onMessage(evt.data);
      }
    };

    this._ws.onerror = () => {
      // onerror always precedes onclose — let onclose handle reconnect
    };

    this._ws.onclose = (evt) => {
      if (this._destroyed) return;
      console.debug(`[WS:${this.agentId}] closed (code=${evt.code})`);
      this._ws = null;
      this._mode = 'idle';

      if (this._retries >= this.maxRetries) {
        console.warn(`[WS:${this.agentId}] max retries reached — switching to polling`);
        this._fallbackToPoll();
        return;
      }

      const delay = Math.min(this.baseDelay * Math.pow(2, this._retries), this.maxDelay);
      this._retries++;
      console.debug(`[WS:${this.agentId}] reconnecting in ${delay}ms (attempt ${this._retries})`);
      this._reconnTimer = setTimeout(() => this._tryWebSocket(), delay);
    };
  }

  // ── POLLING FALLBACK ────────────────────────────────────────────────────────
  _fallbackToPoll() {
    if (this._destroyed || this._pollTimer) return;
    this._mode = 'poll';
    console.debug(`[WS:${this.agentId}] polling every ${this.pollInterval}ms`);
    const poll = async () => {
      if (this._destroyed) return;
      const data = await fetchAgentData(this.agentId);
      if (data) this.onMessage(data);
    };
    poll();
    this._pollTimer = setInterval(poll, this.pollInterval);
  }

  _clearPoll() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  }

  _clearTimers() {
    if (this._reconnTimer) { clearTimeout(this._reconnTimer); this._reconnTimer = null; }
    this._clearPoll();
  }

  get mode() { return this._mode; }
  get connected() { return this._mode === 'ws' && this._ws?.readyState === WebSocket.OPEN; }
}
