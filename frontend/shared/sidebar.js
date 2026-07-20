/**
 * sidebar.js — Universe Ecosystem v3
 * Sidebar collapse/expand, responsive behavior, active nav item highlighting.
 */

const SidebarManager = {
  _collapsed: false,
  _storageKey: 'ue-sidebar-collapsed',

  init() {
    document.addEventListener('DOMContentLoaded', () => {
      // Restore saved state
      const saved = localStorage.getItem(this._storageKey);
      if (saved === '1') this._setCollapsed(true, false);

      // Bind toggle button
      const btn = document.getElementById('sidebarToggle');
      if (btn) btn.addEventListener('click', () => this.toggle());

      // Highlight active nav item
      this._highlightActive();

      // Responsive: auto-collapse on tablet
      this._handleResize();
      window.addEventListener('resize', () => this._handleResize());
    });
  },

  toggle() {
    this._setCollapsed(!this._collapsed);
  },

  _setCollapsed(collapsed, save = true) {
    this._collapsed = collapsed;
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.toggle('collapsed', collapsed);
    if (save) localStorage.setItem(this._storageKey, collapsed ? '1' : '0');
  },

  _highlightActive() {
    const current = window.location.pathname.split('/').pop() || 'index.html';
    document.querySelectorAll('.sidebar-nav-item[href]').forEach(link => {
      const href = link.getAttribute('href');
      if (href === current || (current === '' && href === 'index.html')) {
        link.classList.add('active');
      }
    });
    // Also highlight topbar tabs
    document.querySelectorAll('.topbar-tab[href]').forEach(link => {
      const href = link.getAttribute('href');
      if (href === current || (current === '' && href === 'index.html')) {
        link.classList.add('active');
      }
    });
  },

  _handleResize() {
    const w = window.innerWidth;
    if (w < 1280 && w >= 768) {
      // Tablet: auto-collapse but don't save
      const sidebar = document.getElementById('sidebar');
      if (sidebar && !this._collapsed) sidebar.classList.add('collapsed');
    } else if (w >= 1280) {
      // Desktop: restore saved state
      const sidebar = document.getElementById('sidebar');
      if (sidebar) sidebar.classList.toggle('collapsed', this._collapsed);
    }
  },
};

SidebarManager.init();
