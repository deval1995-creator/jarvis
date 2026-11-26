// Network graph for Jarvis's windows: an animated force layout on a <canvas>.
//
//   new JarvisGraph(canvas, { nodes: [{id, label, group, detail, size}], edges: [{from, to, label}] })
//
// Linked nodes pull together, all nodes push apart, the whole settles into
// place and stays alive: pulses of light travel along the links. Drag nodes to
// rearrange (the layout follows), hover for the full text.
(() => {
  const TAU = Math.PI * 2;
  const KNOWN = {
    person: [255, 196, 92], people: [255, 196, 92],
    topic: [79, 227, 161], category: [79, 227, 161],
    memory: [92, 200, 255], fact: [92, 200, 255],
  };
  const EXTRA = [[190, 140, 255], [255, 120, 160], [120, 235, 230], [240, 240, 140], [160, 200, 255]];
  const groupColour = (g, cache) => {
    if (!g) return [92, 200, 255];
    const k = g.toLowerCase();
    if (KNOWN[k]) return KNOWN[k];
    if (!cache[k]) cache[k] = EXTRA[Object.keys(cache).length % EXTRA.length];
    return cache[k];
  };
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const ELLIPSIS = String.fromCharCode(8230);
  const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + ELLIPSIS : s);

  class JarvisGraph {
    constructor(canvas, graph) {
      this.cv = canvas; this.g = canvas.getContext("2d");
      const extra = {};
      const n = graph.nodes.length;
      this.nodes = graph.nodes.map((d, i) => {
        const a = (i / Math.max(1, n)) * TAU, r = 30 + Math.random() * 40;
        return { ...d, x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0, deg: 0,
                 col: groupColour(d.group, extra), born: performance.now() + i * 18 };
      });
      const byId = new Map(this.nodes.map((d) => [d.id, d]));
      this.edges = graph.edges.map((e) => ({ ...e, a: byId.get(e.from), b: byId.get(e.to), phase: Math.random() }))
        .filter((e) => e.a && e.b);
      for (const e of this.edges) { e.a.deg++; e.b.deg++; }
      for (const d of this.nodes) d.r = 4 + (d.size || 2) * 1.6 + Math.min(10, d.deg * 1.2);
      this.groups = [...new Set(this.nodes.map((d) => d.group).filter(Boolean))].map((gname) => [gname, groupColour(gname, extra)]);
      this.heat = 1; this.hover = null; this.drag = null; this.view = { x: 0, y: 0, k: 1 };
      this.bind();
      this.last = performance.now();
      const loop = (now) => { if (!this.cv.isConnected) return; this.step(now); this.draw(now); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    toWorld(ev) {
      const r = this.cv.getBoundingClientRect();
      return { x: (ev.clientX - r.left - r.width / 2) / this.view.k, y: (ev.clientY - r.top - r.height / 2) / this.view.k };
    }
    pick(p) {
      let best = null, bd = Infinity;
      for (const d of this.nodes) {
        const dd = Math.hypot(d.x - p.x, d.y - p.y);
        if (dd < d.r + 6 && dd < bd) { best = d; bd = dd; }
      }
      return best;
    }
    bind() {
      const cv = this.cv;
      cv.addEventListener("pointermove", (ev) => {
        const p = this.toWorld(ev);
        if (this.drag) { this.drag.x = p.x; this.drag.y = p.y; this.drag.vx = this.drag.vy = 0; this.heat = Math.max(this.heat, 0.5); }
        this.hover = this.drag || this.pick(p);
        cv.style.cursor = this.hover ? "grab" : "default";
      });
      cv.addEventListener("pointerdown", (ev) => {
        const d = this.pick(this.toWorld(ev));
        if (d) { this.drag = d; cv.setPointerCapture(ev.pointerId); ev.stopPropagation(); }
      });
      const up = () => { this.drag = null; };
      cv.addEventListener("pointerup", up); cv.addEventListener("pointercancel", up);
      cv.addEventListener("pointerleave", () => { if (!this.drag) this.hover = null; });
    }

    step(now) {
      const dt = Math.min(0.05, (now - this.last) / 1000); this.last = now;
      if (this.heat < 0.02) return;
      const N = this.nodes, k = this.heat;
      for (let i = 0; i < N.length; i++) {            // repulsion
        const a = N[i];
        for (let j = i + 1; j < N.length; j++) {
          const b = N[j]; let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01;
          if (d2 > 250000) continue;
          const f = 5200 / d2, d = Math.sqrt(d2);
          dx /= d; dy /= d; a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
        }
      }
      for (const e of this.edges) {                    // springs
        const a = e.a, b = e.b, dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
        const rest = 95 + (a.r + b.r), f = (d - rest) * 0.035;
        a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
      for (const d of N) {                             // gravity, damping, move
        d.vx -= d.x * 0.008; d.vy -= d.y * 0.008;
        if (d === this.drag) continue;
        d.vx *= 0.82; d.vy *= 0.82;
        d.x += d.vx * k * dt * 60 * 0.12; d.y += d.vy * k * dt * 60 * 0.12;
      }
      this.heat *= 0.994;
    }

    draw(now) {
      const cv = this.cv, dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
      if (!W || !H) return;
      if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
      // fit everything in view (eased)
      let mx = 1, my = 1;
      for (const d of this.nodes) { mx = Math.max(mx, Math.abs(d.x) + d.r + 60); my = Math.max(my, Math.abs(d.y) + d.r + 24); }
      const target = Math.min(1.6, (W / 2) / mx, (H / 2) / my);
      this.view.k += (target - this.view.k) * 0.08;
      const g = this.g, K = this.view.k, t = now / 1000;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, W, H);
      g.translate(W / 2, H / 2); g.scale(K, K);
      g.globalCompositeOperation = "lighter";

      const focus = this.hover;
      const linked = new Set();
      if (focus) for (const e of this.edges) if (e.a === focus || e.b === focus) { linked.add(e.a); linked.add(e.b); }

      for (const e of this.edges) {                    // links with travelling pulses
        const on = !focus || e.a === focus || e.b === focus;
        g.strokeStyle = rgba(e.a.col, on ? 0.35 : 0.06); g.lineWidth = (on && focus ? 1.6 : 1) / K;
        g.beginPath(); g.moveTo(e.a.x, e.a.y); g.lineTo(e.b.x, e.b.y); g.stroke();
        if (on) {
          const p = (t * 0.35 + e.phase) % 1, x = e.a.x + (e.b.x - e.a.x) * p, y = e.a.y + (e.b.y - e.a.y) * p;
          g.fillStyle = rgba(e.b.col, 0.85); g.beginPath(); g.arc(x, y, 1.8 / K, 0, TAU); g.fill();
        }
        if (on && focus && e.label) {
          g.fillStyle = "rgba(150,190,220,.8)"; g.font = `${10 / K}px ui-monospace, Consolas, monospace`; g.textAlign = "center";
          g.fillText(e.label, (e.a.x + e.b.x) / 2, (e.a.y + e.b.y) / 2 - 3 / K);
        }
      }
      for (const d of this.nodes) {                    // nodes
        const grow = Math.min(1, Math.max(0, (now - d.born) / 500));
        if (!grow) continue;
        const dim = focus && d !== focus && !linked.has(d), r = d.r * (0.4 + 0.6 * grow) * (d === focus ? 1.25 : 1);
        const pulse = 1 + 0.08 * Math.sin(t * 2 + d.x * 0.05);
        const glow = g.createRadialGradient(d.x, d.y, 0, d.x, d.y, r * 3 * pulse);
        glow.addColorStop(0, rgba(d.col, dim ? 0.12 : 0.45)); glow.addColorStop(1, rgba(d.col, 0));
        g.fillStyle = glow; g.beginPath(); g.arc(d.x, d.y, r * 3 * pulse, 0, TAU); g.fill();
        g.fillStyle = rgba(d.col, dim ? 0.25 : 0.95); g.beginPath(); g.arc(d.x, d.y, r, 0, TAU); g.fill();
        g.strokeStyle = rgba([255, 255, 255], dim ? 0.1 : 0.5); g.lineWidth = 1 / K;
        g.beginPath(); g.arc(d.x, d.y, r + 2.5 / K, 0, TAU); g.stroke();
      }
      g.globalCompositeOperation = "source-over";
      g.textAlign = "center"; g.textBaseline = "top";
      for (const d of this.nodes) {                    // labels
        if (now < d.born) continue;
        const dim = focus && d !== focus && !linked.has(d);
        const big = d.r > 9 || this.nodes.length < 30 || d === focus || linked.has(d);
        if (!big && !focus) continue;
        g.font = `${(d.r > 9 ? 12 : 10.5) / K}px ui-monospace, Consolas, monospace`;
        g.fillStyle = dim ? "rgba(140,170,200,.25)" : d === focus ? "#ffffff" : "rgba(200,232,255,.9)";
        g.fillText(short(d.label, d === focus ? 60 : 26), d.x, d.y + d.r + 4 / K);
      }

      g.setTransform(dpr, 0, 0, dpr, 0, 0);            // overlay: legend + hover card
      g.font = "10px ui-monospace, Consolas, monospace"; g.textAlign = "left"; g.textBaseline = "middle";
      this.groups.forEach(([name, col], i) => {
        g.fillStyle = rgba(col, 0.95); g.beginPath(); g.arc(12, 12 + i * 15, 4, 0, TAU); g.fill();
        g.fillStyle = "rgba(160,190,215,.9)"; g.fillText(name.toUpperCase(), 22, 12 + i * 15);
      });
      if (focus) {
        const text = focus.detail || focus.label, words = text.split(/\s+/), lines = [""];
        for (const w of words) { if ((lines[lines.length - 1] + " " + w).length > 52) lines.push(w); else lines[lines.length - 1] += (lines[lines.length - 1] ? " " : "") + w; }
        const h = 14 * lines.length + 12, w = 340, x = W - w - 8, y = H - h - 8;
        g.fillStyle = "rgba(3,14,30,.92)"; g.fillRect(x, y, w, h);
        g.strokeStyle = rgba(focus.col, 0.8); g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
        g.fillStyle = "#cfe9ff";
        lines.slice(0, 8).forEach((ln, i) => g.fillText(ln, x + 8, y + 12 + i * 14));
      }
    }
  }
  window.JarvisGraph = JarvisGraph;
})();
