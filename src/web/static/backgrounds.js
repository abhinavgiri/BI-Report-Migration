/* Animated backgrounds, one scene per theme. Subtle by design: low alpha, slow
   motion, paused when the tab is hidden, and a static frame under reduced motion. */
(function () {
  const canvas = document.getElementById('bg');
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, dpr = 1, scene = null, raf = 0, last = 0, running = true, reduced = false;
  let theme = 'ocean', mode = 'light';

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (scene) scene.init();
  }
  const rnd = (a, b) => a + Math.random() * (b - a);
  const dark = () => mode === 'dark';

  /* ---- Sakura: drifting petals ---- */
  function sakura() {
    let petals = [];
    return {
      init() {
        petals = Array.from({ length: Math.round(W / 22) }, () => ({
          x: rnd(0, W), y: rnd(-H, H), s: rnd(5, 11), r: rnd(0, Math.PI * 2), vr: rnd(-.02, .02),
          vy: rnd(.35, .9), vx: rnd(-.25, .35), sway: rnd(0, Math.PI * 2), a: rnd(.35, .8)
        }));
      },
      step(dt) {
        for (const p of petals) {
          p.sway += .012 * dt; p.x += (p.vx + Math.sin(p.sway) * .5) * dt; p.y += p.vy * dt; p.r += p.vr * dt;
          if (p.y > H + 20 || p.x < -30 || p.x > W + 30) { p.y = -20; p.x = rnd(0, W); }
        }
      },
      draw() {
        // soft branch-like haze at the top corners
        const g = ctx.createRadialGradient(W * .1, 0, 0, W * .1, 0, W * .55);
        g.addColorStop(0, dark() ? 'rgba(236,72,153,.16)' : 'rgba(249,168,212,.35)'); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
        for (const p of petals) {
          ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r);
          ctx.fillStyle = dark() ? `rgba(251,182,206,${p.a * .7})` : `rgba(236,72,153,${p.a * .55})`;
          ctx.beginPath(); ctx.ellipse(0, 0, p.s, p.s * .62, 0, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = dark() ? 'rgba(255,255,255,.18)' : 'rgba(255,255,255,.45)';
          ctx.beginPath(); ctx.ellipse(-p.s * .25, -p.s * .15, p.s * .35, p.s * .2, 0, 0, Math.PI * 2); ctx.fill();
          ctx.restore();
        }
      }
    };
  }

  /* ---- Ocean: layered waves + rising bubbles ---- */
  function ocean() {
    let t = 0, bubbles = [];
    return {
      init() { bubbles = Array.from({ length: 26 }, () => ({ x: rnd(0, W), y: rnd(H * .55, H), r: rnd(1.5, 4.5), v: rnd(.15, .45), a: rnd(.15, .4) })); },
      step(dt) { t += dt * .008; for (const b of bubbles) { b.y -= b.v * dt; b.x += Math.sin(t * 3 + b.r) * .2; if (b.y < H * .5) { b.y = H + 10; b.x = rnd(0, W); } } },
      draw() {
        const base = H * .68;
        const layers = [
          { amp: 18, len: 1.6, sp: 1.0, off: 0, c: dark() ? 'rgba(14,165,233,.10)' : 'rgba(14,165,233,.14)' },
          { amp: 14, len: 1.1, sp: 1.6, off: 40, c: dark() ? 'rgba(99,102,241,.10)' : 'rgba(99,102,241,.12)' },
          { amp: 10, len: .8, sp: 2.3, off: 80, c: dark() ? 'rgba(56,189,248,.12)' : 'rgba(56,189,248,.18)' },
        ];
        for (const L of layers) {
          ctx.beginPath(); ctx.moveTo(0, H);
          for (let x = 0; x <= W; x += 8) {
            const y = base + L.off + Math.sin((x / W) * Math.PI * 2 * L.len + t * L.sp) * L.amp + Math.sin((x / W) * Math.PI * 6 + t * L.sp * .7) * L.amp * .35;
            ctx.lineTo(x, y);
          }
          ctx.lineTo(W, H); ctx.closePath(); ctx.fillStyle = L.c; ctx.fill();
        }
        for (const b of bubbles) { ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2); ctx.strokeStyle = dark() ? `rgba(186,230,253,${b.a})` : `rgba(14,165,233,${b.a})`; ctx.lineWidth = 1; ctx.stroke(); }
      }
    };
  }

  /* ---- Sunset: a slow sun, drifting clouds, warm haze ---- */
  function sunset() {
    let t = 0, clouds = [];
    return {
      init() { clouds = Array.from({ length: 7 }, () => ({ x: rnd(-200, W), y: rnd(H * .12, H * .5), w: rnd(160, 380), h: rnd(28, 56), v: rnd(.08, .22), a: rnd(.18, .35) })); },
      step(dt) { t += dt * .0025; for (const c of clouds) { c.x += c.v * dt; if (c.x > W + 250) c.x = -c.w - 100; } },
      draw() {
        const sky = ctx.createLinearGradient(0, 0, 0, H);
        if (dark()) { sky.addColorStop(0, 'rgba(24,13,18,0)'); sky.addColorStop(.6, 'rgba(249,115,22,.10)'); sky.addColorStop(1, 'rgba(244,63,94,.18)'); }
        else { sky.addColorStop(0, 'rgba(255,246,240,0)'); sky.addColorStop(.55, 'rgba(251,146,60,.16)'); sky.addColorStop(1, 'rgba(244,63,94,.22)'); }
        ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
        const sx = W * .72 + Math.sin(t) * 40, sy = H * .62 + Math.cos(t * .8) * 18, R = Math.min(W, H) * .16;
        const glow = ctx.createRadialGradient(sx, sy, R * .2, sx, sy, R * 2.6);
        glow.addColorStop(0, dark() ? 'rgba(251,146,60,.35)' : 'rgba(251,146,60,.45)'); glow.addColorStop(1, 'rgba(251,146,60,0)');
        ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);
        const sun = ctx.createLinearGradient(sx, sy - R, sx, sy + R);
        sun.addColorStop(0, dark() ? 'rgba(253,186,116,.55)' : 'rgba(253,186,116,.9)'); sun.addColorStop(1, dark() ? 'rgba(244,63,94,.45)' : 'rgba(244,63,94,.75)');
        ctx.fillStyle = sun; ctx.beginPath(); ctx.arc(sx, sy, R, 0, Math.PI * 2); ctx.fill();
        for (const c of clouds) {
          ctx.fillStyle = dark() ? `rgba(255,255,255,${c.a * .25})` : `rgba(255,255,255,${c.a})`;
          for (let i = 0; i < 4; i++) { ctx.beginPath(); ctx.ellipse(c.x + i * c.w * .22, c.y + (i % 2) * 6, c.w * .28, c.h, 0, 0, Math.PI * 2); ctx.fill(); }
        }
      }
    };
  }

  /* ---- Emerald: fireflies and slow leaves ---- */
  function emerald() {
    let flies = [], leaves = [], t = 0;
    return {
      init() {
        flies = Array.from({ length: 34 }, () => ({ x: rnd(0, W), y: rnd(0, H), r: rnd(1.2, 2.6), p: rnd(0, 6), vx: rnd(-.15, .15), vy: rnd(-.12, .12) }));
        leaves = Array.from({ length: 14 }, () => ({ x: rnd(0, W), y: rnd(-H, H), s: rnd(8, 16), r: rnd(0, 6), vr: rnd(-.01, .01), vy: rnd(.2, .5), sway: rnd(0, 6) }));
      },
      step(dt) {
        t += dt * .01;
        for (const f of flies) { f.x += f.vx * dt; f.y += f.vy * dt; f.p += .03 * dt; if (f.x < 0 || f.x > W) f.vx *= -1; if (f.y < 0 || f.y > H) f.vy *= -1; }
        for (const l of leaves) { l.sway += .01 * dt; l.x += Math.sin(l.sway) * .4 * dt; l.y += l.vy * dt; l.r += l.vr * dt; if (l.y > H + 20) { l.y = -20; l.x = rnd(0, W); } }
      },
      draw() {
        const g = ctx.createRadialGradient(W * .85, H * .1, 0, W * .85, H * .1, W * .6);
        g.addColorStop(0, dark() ? 'rgba(16,185,129,.14)' : 'rgba(52,211,153,.22)'); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
        for (const l of leaves) {
          ctx.save(); ctx.translate(l.x, l.y); ctx.rotate(l.r);
          ctx.fillStyle = dark() ? 'rgba(52,211,153,.22)' : 'rgba(5,150,105,.20)';
          ctx.beginPath(); ctx.moveTo(0, -l.s); ctx.quadraticCurveTo(l.s * .9, 0, 0, l.s); ctx.quadraticCurveTo(-l.s * .9, 0, 0, -l.s); ctx.fill();
          ctx.restore();
        }
        for (const f of flies) {
          const a = (Math.sin(f.p) + 1) / 2;
          const gl = ctx.createRadialGradient(f.x, f.y, 0, f.x, f.y, f.r * 6);
          gl.addColorStop(0, dark() ? `rgba(167,243,208,${.55 * a})` : `rgba(16,185,129,${.5 * a})`); gl.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = gl; ctx.beginPath(); ctx.arc(f.x, f.y, f.r * 6, 0, Math.PI * 2); ctx.fill();
        }
      }
    };
  }

  const SCENES = { sakura, ocean, sunset, emerald };

  function frame(ts) {
    raf = 0;
    if (!running) return;
    const dt = Math.min(2.5, (ts - (last || ts)) / 16.67); last = ts;
    ctx.clearRect(0, 0, W, H);
    if (!reduced) scene.step(dt);
    scene.draw();
    if (!reduced) raf = requestAnimationFrame(frame);
  }
  function start() { if (!raf && running) { last = 0; raf = requestAnimationFrame(frame); } }

  window.Backgrounds = {
    set(nextTheme, nextMode) {
      theme = nextTheme; mode = nextMode;
      scene = SCENES[theme] ? SCENES[theme]() : ocean();
      scene.init(); ctx.clearRect(0, 0, W, H); start();
    },
    reduce(flag) { reduced = flag; if (scene) { ctx.clearRect(0, 0, W, H); scene.draw(); if (!reduced) start(); } },
  };
  window.addEventListener('resize', resize);
  document.addEventListener('visibilitychange', () => { running = !document.hidden; if (running) start(); });
  resize();
})();
