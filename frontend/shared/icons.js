/**
 * Universe v8 — Mythic SVG Icon Registry
 * All icons use currentColor, 24×24 viewBox, cosmic/mythic aesthetic
 * Usage: ICONS.agent.nexus  |  ICONS.ui.home  |  injectIcons()
 */
const ICONS = {
  agent: {
    // Nexus — radiant hub with 8 emanating spokes and orbiting nodes
    nexus: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="2.5"/>
      <line x1="12" y1="2" x2="12" y2="6"/>
      <line x1="12" y1="18" x2="12" y2="22"/>
      <line x1="2" y1="12" x2="6" y2="12"/>
      <line x1="18" y1="12" x2="22" y2="12"/>
      <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/>
      <line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
      <line x1="19.07" y1="4.93" x2="16.24" y2="7.76"/>
      <line x1="7.76" y1="16.24" x2="4.93" y2="19.07"/>
      <circle cx="12" cy="2" r="1" fill="currentColor" stroke="none"/>
      <circle cx="12" cy="22" r="1" fill="currentColor" stroke="none"/>
      <circle cx="2" cy="12" r="1" fill="currentColor" stroke="none"/>
      <circle cx="22" cy="12" r="1" fill="currentColor" stroke="none"/>
    </svg>`,

    // Oracle — all-seeing eye with mystical iris rays and cosmic pupil
    oracle: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/>
      <circle cx="12" cy="12" r="3"/>
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>
      <line x1="12" y1="2" x2="12" y2="4.5"/>
      <line x1="12" y1="19.5" x2="12" y2="22"/>
      <line x1="4.5" y1="6.5" x2="6.2" y2="8.2"/>
      <line x1="17.8" y1="15.8" x2="19.5" y2="17.5"/>
      <line x1="19.5" y1="6.5" x2="17.8" y2="8.2"/>
      <line x1="6.2" y1="15.8" x2="4.5" y2="17.5"/>
    </svg>`,

    // Atlas — celestial globe with meridian lines and constellation cross
    atlas: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="9.5"/>
      <ellipse cx="12" cy="12" rx="4" ry="9.5"/>
      <line x1="2.5" y1="12" x2="21.5" y2="12"/>
      <path d="M4.5 7.5 Q12 9 19.5 7.5"/>
      <path d="M4.5 16.5 Q12 15 19.5 16.5"/>
      <circle cx="12" cy="4" r="1" fill="currentColor" stroke="none"/>
      <circle cx="18" cy="8" r="0.8" fill="currentColor" stroke="none"/>
      <circle cx="6" cy="17" r="0.8" fill="currentColor" stroke="none"/>
    </svg>`,

    // Chronicle — ancient tome with glowing rune lines and arcane clasp
    chronicle: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 2h13a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/>
      <line x1="4" y1="2" x2="4" y2="22"/>
      <line x1="8" y1="7" x2="16" y2="7"/>
      <line x1="8" y1="10.5" x2="16" y2="10.5"/>
      <line x1="8" y1="14" x2="13" y2="14"/>
      <path d="M14 16.5 l1.5 1.5 l2.5-3" stroke-width="1.3"/>
      <circle cx="17" cy="4.5" r="1" fill="currentColor" stroke="none"/>
    </svg>`,

    // Sentinel — watchtower beacon with emanating radar waves
    sentinel: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="12,2 15,8 21,8 16.5,12.5 18.5,19 12,15.5 5.5,19 7.5,12.5 3,8 9,8"/>
      <circle cx="12" cy="10.5" r="1.5" fill="currentColor" stroke="none"/>
      <path d="M8.5 14.5 Q12 17 15.5 14.5" stroke-width="1.2"/>
      <path d="M6 16.5 Q12 20 18 16.5" stroke-width="1"/>
    </svg>`,

    // Aegis — mythic shield with arcane diamond rune and protective arcs
    aegis: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 2 L20 5.5 V12 C20 16.5 16.5 20.5 12 22 C7.5 20.5 4 16.5 4 12 V5.5 Z"/>
      <path d="M12 7 l2.5 3.5 L12 17 L9.5 10.5 Z"/>
      <line x1="9.5" y1="10.5" x2="14.5" y2="10.5"/>
      <circle cx="12" cy="7" r="0.8" fill="currentColor" stroke="none"/>
    </svg>`,

    // Forge — cosmic anvil with hammer and spark arcs
    forge: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="4" y="13" width="16" height="5" rx="1"/>
      <path d="M8 13 V9 Q8 6 12 6 Q15 6 16 8"/>
      <path d="M14 5 L18 2"/>
      <path d="M16 7 L20 5"/>
      <circle cx="18.5" cy="2.5" r="0.8" fill="currentColor" stroke="none"/>
      <circle cx="20.5" cy="4.5" r="0.8" fill="currentColor" stroke="none"/>
      <line x1="8" y1="18" x2="8" y2="22"/>
      <line x1="16" y1="18" x2="16" y2="22"/>
      <line x1="12" y1="18" x2="12" y2="22"/>
    </svg>`,

    // Genesis — cosmic seed / sprouting tree of life with DNA helix suggestion
    genesis: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="12" y1="22" x2="12" y2="10"/>
      <path d="M12 10 Q7 8 5 4"/>
      <path d="M12 10 Q17 8 19 4"/>
      <path d="M12 14 Q8 13 6 10"/>
      <path d="M12 14 Q16 13 18 10"/>
      <path d="M12 18 Q9 17.5 8 15.5"/>
      <path d="M12 18 Q15 17.5 16 15.5"/>
      <circle cx="12" cy="8" r="2" fill="currentColor" stroke="none" opacity="0.3"/>
      <circle cx="12" cy="8" r="1" fill="currentColor" stroke="none"/>
      <path d="M10 4 Q12 2 14 4" stroke-width="1.2"/>
    </svg>`,

    // Pulse — rhythmic heartbeat wave with energy nodes
    pulse: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="1,12 5,12 7,6 9,18 11,9 13,15 15,12 19,12 23,12"/>
      <circle cx="7" cy="6" r="1" fill="currentColor" stroke="none"/>
      <circle cx="9" cy="18" r="1" fill="currentColor" stroke="none"/>
      <circle cx="11" cy="9" r="1" fill="currentColor" stroke="none"/>
      <circle cx="13" cy="15" r="1" fill="currentColor" stroke="none"/>
    </svg>`,
  },

  ui: {
    // Home — cosmic command center icon
    home: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9.5z"/>
      <polyline points="9,21 9,13 15,13 15,21"/>
    </svg>`,

    // Chat — speech bubble with cosmic dot
    chat: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      <circle cx="9" cy="10" r="1" fill="currentColor" stroke="none"/>
      <circle cx="12" cy="10" r="1" fill="currentColor" stroke="none"/>
      <circle cx="15" cy="10" r="1" fill="currentColor" stroke="none"/>
    </svg>`,

    // Theme toggle — sun/moon hybrid
    theme: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="4"/>
      <path d="M12 2v2M12 20v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M2 12h2M20 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
    </svg>`,

    // Close — X with cosmic flair
    close: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>`,

    // Menu / hamburger
    menu: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="3" y1="6" x2="21" y2="6"/>
      <line x1="3" y1="12" x2="21" y2="12"/>
      <line x1="3" y1="18" x2="21" y2="18"/>
    </svg>`,

    // Collapse sidebar arrow
    collapse: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15,18 9,12 15,6"/>
    </svg>`,

    // Expand sidebar arrow
    expand: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9,18 15,12 9,6"/>
    </svg>`,

    // Refresh / reload
    refresh: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="23,4 23,10 17,10"/>
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
    </svg>`,

    // Send arrow
    send: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="22" y1="2" x2="11" y2="13"/>
      <polygon points="22,2 15,22 11,13 2,9"/>
    </svg>`,

    // Back arrow
    back: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <line x1="19" y1="12" x2="5" y2="12"/>
      <polyline points="12,19 5,12 12,5"/>
    </svg>`,

    // Status dot / connection
    connected: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 12.55a11 11 0 0 1 14.08 0"/>
      <path d="M1.42 9a16 16 0 0 1 21.16 0"/>
      <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
      <circle cx="12" cy="20" r="1" fill="currentColor" stroke="none"/>
    </svg>`,

    // Dashboard / grid
    dashboard: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1"/>
      <rect x="14" y="3" width="7" height="7" rx="1"/>
      <rect x="3" y="14" width="7" height="7" rx="1"/>
      <rect x="14" y="14" width="7" height="7" rx="1"/>
    </svg>`,

    // Live feed / activity
    feed: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
    </svg>`,

    // Settings / cog
    settings: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>`,
  }
};

/**
 * Inject SVG icons into all [data-icon] elements on the page.
 * Usage: <span data-icon="agent:nexus" class="agent-icon"></span>
 *        <span data-icon="ui:home" class="nav-icon"></span>
 */
function injectIcons() {
  document.querySelectorAll('[data-icon]').forEach(el => {
    const [ns, name] = el.getAttribute('data-icon').split(':');
    const svg = ICONS[ns] && ICONS[ns][name];
    if (svg) {
      el.innerHTML = svg;
      el.classList.add('icon-injected');
    }
  });
}

/**
 * Get an SVG string by namespace:name
 * @param {string} key  e.g. "agent:nexus" or "ui:home"
 * @returns {string} SVG markup
 */
function getIcon(key) {
  const [ns, name] = key.split(':');
  return (ICONS[ns] && ICONS[ns][name]) || '';
}

// Auto-inject on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', injectIcons);
} else {
  injectIcons();
}
