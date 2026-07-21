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

  // Config per theme
  const DARK_CFG = {
    starCount: 180,
    starColor: 'rgba(255,255,255,',
    starMinR: 0.4, starMaxR: 1.8,
    lineColor: 'rgba(255,255,255,',
    lineMaxDist: 120,
    lineMaxAlpha: 0.08,
    bgAlpha: 0,
  };

  const LIGHT_CFG = {
    starCount: 140,
    starColor: 'rgba(200,150,30,',   // golden dust
    starMinR: 0.5, starMaxR: 1.6,
    lineColor: 'rgba(180,130,20,',   // warm amber lines
    lineMaxDist: 110,
    lineMaxAlpha: 0.12,
    bgAlpha: 0,
  };

  function cfg() { return isLight ? LIGHT_CFG : DARK_CFG; }

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }

  function initStars() {
    const c = cfg();
    stars = Array.from({ length: c.starCount }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      r: c.starMinR + Math.random() * (c.starMaxR - c.starMinR),
      vx: (Math.random() - 0.5) * 0.08,
      vy: (Math.random() - 0.5) * 0.08,
      baseAlpha: 0.3 + Math.random() * 0.7,
      alpha: 0,
      phase: Math.random() * Math.PI * 2,
      speed: 0.003 + Math.random() * 0.007,
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
      ctx.fillStyle = c.starColor + s.alpha + ')';
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
          ctx.strokeStyle = c.lineColor + alpha + ')';
          ctx.lineWidth = 0.5;
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
