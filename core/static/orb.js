// Jarvis's orb: an Iron Man-style HUD reactor drawn on a <canvas>.
//
//   new JarvisOrb(canvas, () => ({ state, mic, out, spectrum }))
//
// state: off | connecting | idle | listening | thinking | speaking
// mic/out: loudness 0..1 of your voice / Jarvis's voice
// spectrum: Uint8Array of frequency bins (the voice ring follows it)
//
// Layers, outside in: bearing ring with degree labels, dashed rings, heavy
// segmented arcs, radar sweep, voice-spectrum ring, plexus particle sphere,
// orbiting satellites, glowing core with lens flare, and a shockwave on every
// state change. All light is additive, and motion is eased and frame-rate
// independent.
(() => {
  const TAU = Math.PI * 2;
  const PALETTE = {
    idle:      { main: [92, 200, 255],  hot: [200, 240, 255], accent: [92, 200, 255] },
    listening: { main: [130, 220, 255], hot: [235, 250, 255], accent: [160, 235, 255] },
    thinking:  { main: [92, 200, 255],  hot: [255, 225, 170], accent: [255, 181, 71] },
    speaking:  { main: [92, 200, 255],  hot: [210, 245, 255], accent: [120, 225, 255] },
    off:       { main: [70, 130, 170],  hot: [150, 190, 215], accent: [70, 130, 170] },
  };
  const mix = (a, b, t) => a.map((x, i) => x + (b[i] - x) * t);
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a)).toFixed(3)})`;

  // Points spread evenly over a sphere, and the pairs close enough to link.
  function sphere(n) {
    const pts = [];
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = i * Math.PI * (3 - Math.sqrt(5));
      pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
    }
    return pts;
  }
  const DUST = sphere(900);
  const NODES = sphere(150);
  const LINKS = [];
  for (let i = 0; i < NODES.length; i++)
    for (let j = i + 1; j < NODES.length; j++) {
      const [a, b] = [NODES[i], NODES[j]];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
      if (d < 0.34) LINKS.push([i, j]);
    }

  class JarvisOrb {
    constructor(canvas, input) {
      this.cv = canvas; this.g = canvas.getContext("2d"); this.input = input;
      this.t = 0; this.last = performance.now();
      this.energy = 0; this.speed = 1; this.heat = 0;   // eased
      this.col = PALETTE.idle;
      this.bars = new Float32Array(96);
      this.waves = [];                                   // shockwaves
      this.prevState = null;
      this.rot = { bearing: 0, dash1: 0, dash2: 0, arcs: 0, arcs2: 0, sweep: 0, sphere: 0, sats: 0 };
      requestAnimationFrame((now) => this.frame(now));
    }

    frame(now) {
      const dt = Math.min(0.05, (now - this.last) / 1000); this.last = now; this.t += dt;
      const inp = this.input();
      const st = inp.state === "connecting" ? "idle" : inp.state;
      if (st !== this.prevState) { if (this.prevState !== null) this.waves.push({ r: 0.3, a: 0.9 }); this.prevState = st; }

      // Eased drives
      const level = st === "speaking" ? inp.out : st === "listening" ? inp.mic : 0;
      const breathe = 0.06 + 0.04 * Math.sin(this.t * 1.6);
      const targetE = st === "thinking" ? 0.35 + 0.1 * Math.sin(this.t * 6) : Math.max(breathe, level);
      this.energy += (targetE - this.energy) * Math.min(1, dt * (targetE > this.energy ? 18 : 5));
      const targetSpeed = st === "thinking" ? 3.2 : st === "speaking" ? 1.4 + level * 2 : st === "listening" ? 1.3 : st === "off" ? 0.35 : 1;
      this.speed += (targetSpeed - this.speed) * Math.min(1, dt * 3);
      this.heat += ((st === "thinking" ? 1 : 0) - this.heat) * Math.min(1, dt * 3);
      const pal = PALETTE[st] || PALETTE.idle;
      this.col = {
        main: mix(this.col.main, pal.main, Math.min(1, dt * 4)),
        hot: mix(this.col.hot, pal.hot, Math.min(1, dt * 4)),
        accent: mix(this.col.accent, pal.accent, Math.min(1, dt * 4)),
      };

      const s = this.speed * dt, r = this.rot;
      r.bearing += 0.05 * s; r.dash1 -= 0.12 * s; r.dash2 += 0.2 * s; r.arcs += 0.35 * s; r.arcs2 -= 0.22 * s;
      r.sweep += 1.1 * s; r.sphere += 0.25 * s + level * dt * 0.6; r.sats += 0.6 * s;

      // Voice ring bars follow the spectrum (or a slow idle wave)
      const spec = inp.spectrum;
      for (let i = 0; i < this.bars.length; i++) {
        const k = i < 48 ? i : 95 - i;                    // mirrored left/right
        let v = 0;
        if (spec && (st === "speaking" || st === "listening")) v = (spec[2 + Math.floor(k * 1.6)] || 0) / 255;
        else v = 0.08 + 0.06 * Math.sin(this.t * 2 + i * 0.4) * (st === "thinking" ? 2.5 : 1);
        this.bars[i] += (v - this.bars[i]) * Math.min(1, dt * (v > this.bars[i] ? 25 : 8));
      }
      for (const w of this.waves) { w.r += dt * 0.9; w.a -= dt * 1.1; }
      this.waves = this.waves.filter((w) => w.a > 0);

      this.draw();
      requestAnimationFrame((n) => this.frame(n));
    }

    draw() {
      const cv = this.cv, dpr = window.devicePixelRatio || 1, W = cv.clientWidth;
      if (!W) return;
      if (cv.width !== Math.round(W * dpr)) cv.width = cv.height = Math.round(W * dpr);
      const g = this.g, c = W / 2, R = W * 0.46, { main, hot, accent } = this.col, E = this.energy, r = this.rot;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.globalCompositeOperation = "source-over";
      g.clearRect(0, 0, W, W);
      g.globalCompositeOperation = "lighter";
      g.lineCap = "round";

      // Ambient glow
      let grd = g.createRadialGradient(c, c, 0, c, c, R * 1.05);
      grd.addColorStop(0, rgba(main, 0.22 + E * 0.35));
      grd.addColorStop(0.45, rgba(main, 0.07 + E * 0.1));
      grd.addColorStop(1, rgba(main, 0));
      g.fillStyle = grd; g.beginPath(); g.arc(c, c, R * 1.05, 0, TAU); g.fill();

      g.save(); g.translate(c, c);

      // 1. Bearing ring: fine ticks, long ticks every 10°, labels every 30°
      g.save(); g.rotate(r.bearing);
      for (let i = 0; i < 180; i++) {
        const a = (i / 180) * TAU, long = i % 5 === 0, rr = R * 0.985;
        g.strokeStyle = rgba(main, long ? 0.75 : 0.28); g.lineWidth = long ? 1.6 : 1;
        g.beginPath(); g.moveTo(Math.cos(a) * rr, Math.sin(a) * rr);
        const len = long ? R * 0.035 : R * 0.018;
        g.lineTo(Math.cos(a) * (rr - len), Math.sin(a) * (rr - len)); g.stroke();
      }
      g.fillStyle = rgba(main, 0.55); g.font = `${Math.max(8, R * 0.03)}px ui-monospace, Consolas, monospace`;
      g.textAlign = "center"; g.textBaseline = "middle";
      for (let d = 0; d < 360; d += 30) {
        const a = (d / 360) * TAU - Math.PI / 2;
        g.save(); g.rotate(a + Math.PI / 2); g.fillText(String(d).padStart(3, "0"), 0, -R * 0.915); g.restore();
      }
      g.restore();

      // 2. Thin solid ring + dashed counter-rotating rings
      g.strokeStyle = rgba(main, 0.35); g.lineWidth = 1;
      g.beginPath(); g.arc(0, 0, R * 0.88, 0, TAU); g.stroke();
      g.save(); g.rotate(r.dash1); g.setLineDash([2, 6]); g.strokeStyle = rgba(main, 0.55); g.lineWidth = 2;
      g.beginPath(); g.arc(0, 0, R * 0.855, 0, TAU); g.stroke(); g.restore();
      g.save(); g.rotate(r.dash2); g.setLineDash([R * 0.12, R * 0.04, R * 0.02, R * 0.04]); g.strokeStyle = rgba(main, 0.3); g.lineWidth = 1;
      g.beginPath(); g.arc(0, 0, R * 0.62, 0, TAU); g.stroke(); g.restore();
      g.setLineDash([]);

      // 3. Heavy segmented arcs (two counter-rotating bands), glowing
      g.save(); g.rotate(r.arcs);
      g.shadowColor = rgba(accent, 1); g.shadowBlur = 14 + E * 20;
      g.strokeStyle = rgba(accent, 0.85); g.lineWidth = R * 0.028;
      for (const [a0, len] of [[0, 1.1], [1.35, 0.45], [2.05, 0.9], [3.3, 1.3], [4.85, 0.6], [5.6, 0.35]]) {
        g.beginPath(); g.arc(0, 0, R * 0.79, a0, a0 + len); g.stroke();
      }
      g.restore();
      g.save(); g.rotate(r.arcs2);
      g.strokeStyle = rgba(main, 0.6); g.lineWidth = R * 0.01;
      for (let i = 0; i < 3; i++) { const a0 = (i / 3) * TAU; g.beginPath(); g.arc(0, 0, R * 0.735, a0, a0 + 1.5); g.stroke(); }
      // little chevrons riding this band
      g.fillStyle = rgba(hot, 0.9);
      for (let i = 0; i < 3; i++) {
        const a = (i / 3) * TAU + 1.6, x = Math.cos(a) * R * 0.735, y = Math.sin(a) * R * 0.735;
        g.save(); g.translate(x, y); g.rotate(a + Math.PI / 2);
        g.beginPath(); g.moveTo(-5, 3); g.lineTo(0, -4); g.lineTo(5, 3); g.closePath(); g.fill(); g.restore();
      }
      g.restore();
      g.shadowBlur = 0;

      // 4. Radar sweep
      if (g.createConicGradient) {
        const sw = g.createConicGradient(r.sweep, 0, 0);
        sw.addColorStop(0, rgba(accent, 0.28 + this.heat * 0.15));
        sw.addColorStop(0.09, rgba(accent, 0));
        sw.addColorStop(1, rgba(accent, 0));
        g.fillStyle = sw; g.beginPath(); g.arc(0, 0, R * 0.86, 0, TAU); g.arc(0, 0, R * 0.64, 0, TAU, true); g.fill();
        g.strokeStyle = rgba(hot, 0.5); g.lineWidth = 1.2;
        g.beginPath(); g.moveTo(Math.cos(r.sweep) * R * 0.64, Math.sin(r.sweep) * R * 0.64);
        g.lineTo(Math.cos(r.sweep) * R * 0.86, Math.sin(r.sweep) * R * 0.86); g.stroke();
      }

      // 5. Voice ring: spectrum bars
      const n = this.bars.length, base = R * 0.57;
      g.lineWidth = Math.max(1.5, R * 0.012);
      for (let i = 0; i < n; i++) {
        const a = (i / n) * TAU - Math.PI / 2, v = this.bars[i];
        const len = R * (0.015 + v * 0.16);
        g.strokeStyle = rgba(v > 0.45 ? hot : main, 0.35 + v * 0.65);
        g.beginPath(); g.moveTo(Math.cos(a) * base, Math.sin(a) * base);
        g.lineTo(Math.cos(a) * (base + len), Math.sin(a) * (base + len)); g.stroke();
      }
      g.strokeStyle = rgba(main, 0.4); g.lineWidth = 1;
      g.beginPath(); g.arc(0, 0, base - 3, 0, TAU); g.stroke();

      // 6. Plexus sphere: dust + linked nodes, depth-shaded
      const sr = R * (0.44 + E * 0.06), cy = Math.cos(r.sphere), sy = Math.sin(r.sphere);
      const tilt = 0.4 + 0.1 * Math.sin(this.t * 0.3), ct = Math.cos(tilt), stt = Math.sin(tilt);
      const project = ([x, y, z], wob) => {
        const x1 = x * cy - z * sy, z1 = x * sy + z * cy;
        const y2 = y * ct - z1 * stt, z2 = y * stt + z1 * ct;
        const k = sr * (1 + wob);
        return [x1 * k, y2 * k, (z2 + 1) / 2];
      };
      for (const p of DUST) {
        const wob = E * 0.07 * Math.sin(p[0] * 7 + p[1] * 5 + this.t * 5);
        const [x, y, d] = project(p, wob);
        g.fillStyle = rgba(main, 0.06 + d * 0.5);
        const sz = 0.6 + d * 1.2; g.fillRect(x, y, sz, sz);
      }
      const P = NODES.map((p) => project(p, E * 0.05 * Math.sin(p[1] * 6 + this.t * 4)));
      g.lineWidth = 0.7;
      for (const [i, j] of LINKS) {
        const a = P[i], b = P[j], d = (a[2] + b[2]) / 2;
        if (d < 0.35) continue;
        g.strokeStyle = rgba(main, (d - 0.35) * 0.55 * (0.6 + E));
        g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke();
      }
      for (const [x, y, d] of P) {
        if (d < 0.4) continue;
        g.fillStyle = rgba(hot, d * 0.9);
        g.beginPath(); g.arc(x, y, 0.8 + d * 1.6, 0, TAU); g.fill();
      }

      // 7. Satellites on tilted orbits
      for (let i = 0; i < 3; i++) {
        const a = r.sats * (1 + i * 0.35) + i * 2.1, rx = R * (0.5 + i * 0.045), ry = rx * (0.35 + i * 0.12);
        g.save(); g.rotate(i * 1.05 + 0.3);
        g.strokeStyle = rgba(main, 0.12); g.lineWidth = 1;
        g.beginPath(); g.ellipse(0, 0, rx, ry, 0, 0, TAU); g.stroke();
        const x = Math.cos(a) * rx, y = Math.sin(a) * ry;
        g.shadowColor = rgba(hot, 1); g.shadowBlur = 10;
        g.fillStyle = rgba(hot, 0.95); g.beginPath(); g.arc(x, y, 2.2, 0, TAU); g.fill();
        g.shadowBlur = 0; g.restore();
      }

      // 8. Core with lens flare
      const coreR = R * (0.07 + E * 0.07);
      grd = g.createRadialGradient(0, 0, 0, 0, 0, coreR * 3.2);
      grd.addColorStop(0, rgba([255, 255, 255], 0.95));
      grd.addColorStop(0.18, rgba(hot, 0.85));
      grd.addColorStop(0.45, rgba(accent, 0.35));
      grd.addColorStop(1, rgba(accent, 0));
      g.fillStyle = grd; g.beginPath(); g.arc(0, 0, coreR * 3.2, 0, TAU); g.fill();
      const flare = R * (0.5 + E * 0.6);
      grd = g.createLinearGradient(-flare, 0, flare, 0);
      grd.addColorStop(0, rgba(hot, 0)); grd.addColorStop(0.5, rgba(hot, 0.55 + E * 0.4)); grd.addColorStop(1, rgba(hot, 0));
      g.fillStyle = grd; g.fillRect(-flare, -0.8, flare * 2, 1.6);
      grd = g.createLinearGradient(0, -flare * 0.35, 0, flare * 0.35);
      grd.addColorStop(0, rgba(hot, 0)); grd.addColorStop(0.5, rgba(hot, 0.3 + E * 0.3)); grd.addColorStop(1, rgba(hot, 0));
      g.fillStyle = grd; g.fillRect(-0.6, -flare * 0.35, 1.2, flare * 0.7);

      // 9. Shockwaves on state change
      for (const w of this.waves) {
        g.strokeStyle = rgba(hot, w.a * 0.8); g.lineWidth = 2 + w.a * 3;
        g.beginPath(); g.arc(0, 0, R * w.r, 0, TAU); g.stroke();
      }

      // 10. Corner brackets framing the reactor
      g.strokeStyle = rgba(main, 0.5); g.lineWidth = 1.5;
      const B = R * 1.06, L = R * 0.09;
      for (const [sx, sy2] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
        g.beginPath(); g.moveTo(sx * B, sy2 * (B - L)); g.lineTo(sx * B, sy2 * B); g.lineTo(sx * (B - L), sy2 * B); g.stroke();
      }

      g.restore();
      g.globalCompositeOperation = "source-over";
    }
  }
  window.JarvisOrb = JarvisOrb;
})();
