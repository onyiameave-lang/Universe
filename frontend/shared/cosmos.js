/**
 * Universe v8 — Cosmos Canvas
 * Dark mode: white stars + constellation lines on void black
 * Light mode: golden dust particles + warm constellation lines on parchment
 */
(function() {
  const canvas = document.getElementById('cosmos-canvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  let W, H, stars = [], lines = [], animId;
  let isLight = document.documentElement.getAttribute('data-theme') === 'light';
  let paused = false;

  // Multi-tone jewel palettes per theme
  const DARK_CFG = {
    starCount: 165,
    starColors: [
      'rgba(248, 250, 252,', // astral white
      'rgba(56, 189, 248,',  // celestial cyan
      'rgba(16, 185, 129,',  // sovereign emerald
      'rgba(168, 85, 247,',  // imperial amethyst
      'rgba(245, 158, 11,',  // editorial amber
      'rgba(236, 72, 153,'   // electric rose
    ],
    starMinR: 0.45, starMaxR: 1.9,
    lineColor: 'rgba(148, 163, 184,',
    lineMaxDist: 118,
    lineMaxAlpha: 0.08,
    bgAlpha: 0,
  };

  const LIGHT_CFG = {
    starCount: 125,
    starColors: [
      'rgba(180, 130, 35,',  // warm gold ink
      'rgba(5, 150, 105,',   // deep emerald ink
      'rgba(2, 132, 199,',   // deep sapphire ink
      'rgba(124, 58, 237,',  // deep amethyst ink
      'rgba(217, 119, 6,'    // amber ink
    ],
    starMinR: 0.55, starMaxR: 1.65,
    lineColor: 'rgba(140, 108, 45,',
    lineMaxDist: 108,
    lineMaxAlpha: 0.10,
    bgAlpha: 0,
  };

  function cfg() { return isLight ? LIGHT_CFG : DARK_CFG; }

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }

  function initStars() {
    const c = cfg();
    stars = Array.from({ length: c.starCount }, (_, idx) => ({
      x: Math.random() * W,
      y: Math.random() * H,
      r: c.starMinR + Math.random() * (c.starMaxR - c.starMinR),
      vx: (Math.random() - 0.5) * 0.08,
      vy: (Math.random() - 0.5) * 0.08,
      baseAlpha: 0.28 + Math.random() * 0.65,
      alpha: 0,
      phase: Math.random() * Math.PI * 2,
      speed: 0.003 + Math.random() * 0.007,
      colorPrefix: c.starColors[idx % c.starColors.length],
    }));
  }

  function draw(ts) {
    if (paused) { animId = requestAnimationFrame(draw); return; }
    ctx.clearRect(0, 0, W, H);
    const c = cfg();
    const t = ts * 0.001;

    // Update + draw stars
    for (const s of stars) {
      s.x += s.vx;
      s.y += s.vy;
      if (s.x < -2) s.x = W + 2;
      if (s.x > W + 2) s.x = -2;
      if (s.y < -2) s.y = H + 2;
      if (s.y > H + 2) s.y = -2;

      // Breathing opacity
      s.alpha = s.baseAlpha * (0.5 + 0.5 * Math.sin(t * s.speed * 60 + s.phase));

      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fillStyle = (s.colorPrefix || c.starColors[0]) + s.alpha.toFixed(3) + ')';
      ctx.fill();
    }

    // Draw constellation lines between nearby stars
    for (let i = 0; i < stars.length; i++) {
      for (let j = i + 1; j < stars.length; j++) {
        const dx = stars[i].x - stars[j].x;
        const dy = stars[i].y - stars[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < c.lineMaxDist) {
          const alpha = c.lineMaxAlpha * (1 - dist / c.lineMaxDist) * Math.min(stars[i].alpha, stars[j].alpha);
          ctx.beginPath();
          ctx.moveTo(stars[i].x, stars[i].y);
          ctx.lineTo(stars[j].x, stars[j].y);
          ctx.strokeStyle = c.lineColor + alpha.toFixed(3) + ')';
          ctx.lineWidth = 0.55;
          ctx.stroke();
        }
      }
    }

    animId = requestAnimationFrame(draw);
  }

  function start() {
    resize();
    initStars();
    if (animId) cancelAnimationFrame(animId);
    animId = requestAnimationFrame(draw);
  }

  // Public API for theme.js to call
  window.CosmosSetTheme = function(theme) {
    isLight = (theme === 'light');
    initStars(); // re-init with new color config
  };

  // Pause when tab hidden (perf)
  document.addEventListener('visibilitychange', () => {
    paused = document.hidden;
  });

  window.addEventListener('resize', () => { resize(); initStars(); });

  start();
})();
