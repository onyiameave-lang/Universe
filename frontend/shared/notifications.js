/**
 * notifications.js — Universe Ecosystem v4
 * Toast notification system with connection status toasts.
 */

const Notifications = (() => {
  let _container = null;

  function _getContainer() {
    if (_container && document.body.contains(_container)) return _container;
    _container = document.createElement('div');
    _container.id = 'toastContainer';
    _container.style.cssText = `
      position:fixed; bottom:24px; right:24px; z-index:9999;
      display:flex; flex-direction:column; gap:8px; pointer-events:none;
      max-width:360px;`;
    document.body.appendChild(_container);
    return _container;
  }

  function _show(type, title, message, duration = 4000) {
    const container = _getContainer();
    const icons = { success:'✅', error:'❌', warn:'⚠️', info:'ℹ️', demo:'🎭' };
    const colors = {
      success: '#10B981', error: '#EF4444',
      warn: '#F59E0B', info: '#6366F1', demo: '#F59E0B'
    };

    const toast = document.createElement('div');
    toast.style.cssText = `
      background: var(--bg-surface, #2F2F2F);
      border: 1px solid var(--border, rgba(255,255,255,0.1));
      border-left: 3px solid ${colors[type] || colors.info};
      border-radius: 10px;
      padding: 12px 16px;
      display: flex; align-items: flex-start; gap: 10px;
      pointer-events: all; cursor: pointer;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      opacity: 0; transform: translateX(20px);
      transition: opacity 0.2s ease, transform 0.2s ease;
      max-width: 360px; min-width: 240px;`;

    toast.innerHTML = `
      <span style="font-size:16px;flex-shrink:0;margin-top:1px">${icons[type] || icons.info}</span>
      <div style="flex:1;min-width:0">
        <div style="font-weight:600;font-size:13px;color:var(--text-primary,#fff);margin-bottom:2px">${title}</div>
        ${message ? `<div style="font-size:12px;color:var(--text-muted,#9B9BAA);line-height:1.4;word-break:break-word">${message}</div>` : ''}
      </div>
      <button onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--text-muted,#9B9BAA);cursor:pointer;font-size:16px;padding:0;line-height:1;flex-shrink:0">×</button>`;

    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => requestAnimationFrame(() => {
      toast.style.opacity = '1';
      toast.style.transform = 'translateX(0)';
    }));

    // Auto-dismiss
    if (duration > 0) {
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 220);
      }, duration);
    }

    // Click to dismiss
    toast.addEventListener('click', (e) => {
      if (e.target.tagName === 'BUTTON') return;
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      setTimeout(() => toast.remove(), 220);
    });

    return toast;
  }

  return {
    success: (title, msg, dur) => _show('success', title, msg, dur),
    error:   (title, msg, dur) => _show('error',   title, msg, dur),
    warn:    (title, msg, dur) => _show('warn',     title, msg, dur),
    info:    (title, msg, dur) => _show('info',     title, msg, dur),
    demo:    (title, msg, dur) => _show('demo',     title, msg, dur),
  };
})();
