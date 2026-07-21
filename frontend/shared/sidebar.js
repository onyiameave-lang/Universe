/**
 * Universe v8 — Sidebar Manager
 * Builds the sidebar nav with SVG icons from ICONS registry.
 * Handles collapse/expand with localStorage persistence.
 */

const AGENTS_NAV = [
  { id: 'nexus',     name: 'Nexus',     role: 'Orchestrator',  icon: 'agent:nexus',     color: '59,130,246',  href: 'nexus.html' },
  { id: 'oracle',    name: 'Oracle',    role: 'Trading',       icon: 'agent:oracle',    color: '16,185,129',  href: 'oracle.html' },
  { id: 'atlas',     name: 'Atlas',     role: 'Research',      icon: 'agent:atlas',     color: '14,165,233',  href: 'atlas.html' },
  { id: 'chronicle', name: 'Chronicle', role: 'History',       icon: 'agent:chronicle', color: '139,92,246',  href: 'chronicle.html' },
  { id: 'sentinel',  name: 'Sentinel',  role: 'Monitoring',    icon: 'agent:sentinel',  color: '245,158,11',  href: 'sentinel.html' },
  { id: 'aegis',     name: 'Aegis',     role: 'Security',      icon: 'agent:aegis',     color: '249,115,22',  href: 'aegis.html' },
  { id: 'forge',     name: 'Forge',     role: 'Builder',       icon: 'agent:forge',     color: '100,116,139', href: 'forge.html' },
  { id: 'genesis',   name: 'Genesis',   role: 'Strategy',      icon: 'agent:genesis',   color: '20,184,166',  href: 'genesis.html' },
  { id: 'pulse',     name: 'Pulse',     role: 'Sentiment',     icon: 'agent:pulse',     color: '236,72,153',  href: 'pulse.html' },
];

function buildSidebarNav(activeAgentId) {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  const collapsed = localStorage.getItem('sidebar-collapsed') === 'true';
  if (collapsed) sidebar.classList.add('collapsed');

  sidebar.innerHTML = `
    <div class="sidebar-header">
      <div class="sidebar-logo-icon icon-md">
        <span data-icon="agent:nexus"></span>
      </div>
      <span class="sidebar-logo-text">Universe</span>
      <button class="sidebar-collapse-btn icon-sm" id="sidebarCollapseBtn" title="Toggle sidebar">
        <span data-icon="ui:collapse"></span>
      </button>
    </div>

    <nav class="sidebar-nav">
      <div class="sidebar-section-label">Navigation</div>

      <a href="index.html" class="sidebar-nav-item ${!activeAgentId ? 'active' : ''}" title="Command Center">
        <span class="sidebar-nav-icon icon-md" data-icon="ui:home"></span>
        <span class="sidebar-nav-label">Command Center</span>
      </a>

      <div class="sidebar-divider"></div>
      <div class="sidebar-section-label">Constellation</div>

      ${AGENTS_NAV.map(agent => `
        <a href="${agent.href}"
           class="sidebar-nav-item ${agent.id === activeAgentId ? 'active' : ''}"
           data-agent-id="${agent.id}"
           style="--agent-rgb: ${agent.color};"
           title="${agent.name} — ${agent.role}">
          <span class="sidebar-nav-icon icon-md" data-icon="${agent.icon}"></span>
          <span class="sidebar-nav-label">${agent.name}</span>
        </a>
      `).join('')}

      <div class="sidebar-divider"></div>

      <button class="sidebar-nav-item" id="sidebarThemeBtn" title="Toggle theme" style="width:100%;background:none;border:none;cursor:pointer;text-align:left;">
        <span class="sidebar-nav-icon icon-md" data-icon="ui:theme"></span>
        <span class="sidebar-nav-label">Toggle Theme</span>
      </button>
    </nav>
  `;

  // Inject icons
  if (typeof injectIcons === 'function') injectIcons();

  // Collapse toggle
  const collapseBtn = document.getElementById('sidebarCollapseBtn');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', () => {
      sidebar.classList.toggle('collapsed');
      const isCollapsed = sidebar.classList.contains('collapsed');
      localStorage.setItem('sidebar-collapsed', isCollapsed);
      // Flip arrow icon
      const iconEl = collapseBtn.querySelector('[data-icon]');
      if (iconEl) {
        iconEl.setAttribute('data-icon', isCollapsed ? 'ui:expand' : 'ui:collapse');
        if (typeof injectIcons === 'function') injectIcons();
      }
    });
    // Set correct initial arrow
    if (collapsed) {
      const iconEl = collapseBtn.querySelector('[data-icon]');
      if (iconEl) {
        iconEl.setAttribute('data-icon', 'ui:expand');
        if (typeof injectIcons === 'function') injectIcons();
      }
    }
  }

  // Theme toggle
  const themeBtn = document.getElementById('sidebarThemeBtn');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      if (typeof window.toggleTheme === 'function') window.toggleTheme();
    });
  }
}

// Auto-init if sidebar element exists
document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.getElementById('sidebar');
  if (sidebar) {
    const agentId = sidebar.getAttribute('data-active-agent') || null;
    buildSidebarNav(agentId);
  }
});
