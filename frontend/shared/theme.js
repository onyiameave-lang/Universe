/**
 * Universe v8 — Theme Engine
 * Runs immediately (before DOMContentLoaded) to prevent flash.
 * Supports dark/light with BroadcastChannel cross-tab sync.
 * Calls CosmosSetTheme() and injectIcons() on theme change.
 */
(function() {
  const STORAGE_KEY = 'universe-theme';
  const CHANNEL_NAME = 'universe-theme-sync';

  // Apply theme to <html> immediately (no flash)
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(STORAGE_KEY, theme);
  }

  // Get saved or system preference
  function getInitialTheme() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  // Apply immediately
  const initial = getInitialTheme();
  applyTheme(initial);

  // BroadcastChannel for cross-tab sync
  let bc;
  try { bc = new BroadcastChannel(CHANNEL_NAME); } catch(e) {}

  function setTheme(theme, broadcast) {
    applyTheme(theme);

    // Update cosmos canvas
    if (typeof window.CosmosSetTheme === 'function') {
      window.CosmosSetTheme(theme);
    }

    // Re-inject icons (they use currentColor, no change needed, but call for completeness)
    if (typeof window.injectIcons === 'function') {
      window.injectIcons();
    }

    // Update Chart.js defaults if present
    if (window.Chart) {
      const isDark = theme === 'dark';
      Chart.defaults.color = isDark ? '#8892a4' : '#7a6540';
      Chart.defaults.borderColor = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(180,130,30,0.15)';
    }

    if (broadcast && bc) {
      bc.postMessage({ theme });
    }
  }

  // Listen for cross-tab changes
  if (bc) {
    bc.onmessage = (e) => {
      if (e.data && e.data.theme) setTheme(e.data.theme, false);
    };
  }

  // Toggle function for theme buttons
  window.toggleTheme = function() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    setTheme(current === 'dark' ? 'light' : 'dark', true);
  };

  // Expose current theme getter
  window.getTheme = function() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  };

  // Wire up theme toggle buttons after DOM ready
  document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('[data-theme-toggle], #themeToggle, .theme-toggle-btn').forEach(btn => {
      btn.addEventListener('click', window.toggleTheme);
    });

    // Cosmos: apply current theme (canvas may not exist yet at script load time)
    const theme = window.getTheme();
    if (typeof window.CosmosSetTheme === 'function') {
      window.CosmosSetTheme(theme);
    }
  });
})();
