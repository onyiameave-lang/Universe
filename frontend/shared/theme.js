/**
 * theme.js — Universe Ecosystem v3
 * Dark/light mode with BroadcastChannel cross-tab sync.
 * Apply IMMEDIATELY (before DOMContentLoaded) to prevent flash.
 */

// ── IMMEDIATE APPLY (no flash) ────────────────────────────────────────────────
(function applyThemeImmediately() {
  const saved = localStorage.getItem('ue-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
})();

// ── BROADCAST CHANNEL ─────────────────────────────────────────────────────────
const _themeChannel = (() => {
  try { return new BroadcastChannel('ue-theme'); } catch (_) { return null; }
})();

if (_themeChannel) {
  _themeChannel.onmessage = (evt) => {
    if (evt.data?.type === 'theme-change') {
      _applyTheme(evt.data.theme, false); // don't re-broadcast
    }
  };
}

// ── CORE ──────────────────────────────────────────────────────────────────────
function _applyTheme(theme, broadcast = true) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('ue-theme', theme);

  // Update button label
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = theme === 'dark' ? '☀️ Light' : '🌙 Dark';

  // Update Chart.js grid colors if charts exist
  if (typeof Chart !== 'undefined') {
    const gridColor = theme === 'dark' ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.06)';
    const tickColor = theme === 'dark' ? '#6B6B7B' : '#9B9BAB';
    Chart.defaults.color = tickColor;
    Chart.defaults.borderColor = gridColor;
    // Re-render all charts
    Object.values(Chart.instances || {}).forEach(chart => {
      if (chart.options?.scales) {
        Object.values(chart.options.scales).forEach(scale => {
          if (scale.grid) scale.grid.color = gridColor;
          if (scale.ticks) scale.ticks.color = tickColor;
        });
      }
      chart.update('none');
    });
  }

  // Notify page-level callback
  if (typeof onThemeChange === 'function') onThemeChange(theme);

  // Broadcast to other tabs
  if (broadcast && _themeChannel) {
    _themeChannel.postMessage({ type: 'theme-change', theme });
  }
}

/**
 * Toggle between dark and light. Call from onclick="toggleTheme()".
 */
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  _applyTheme(current === 'dark' ? 'light' : 'dark');
}

/**
 * Get current theme.
 * @returns {'dark'|'light'}
 */
function getTheme() {
  return document.documentElement.getAttribute('data-theme') || 'dark';
}

// ── INIT BUTTON LABEL ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const theme = getTheme();
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = theme === 'dark' ? '☀️ Light' : '🌙 Dark';
});
