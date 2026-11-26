// Charts for Jarvis's windows, drawn on a <canvas> in the HUD style.
//
//   new JarvisChart(canvas, { kind, x, series: [{name, values}], unit, stacked })
//
// kind: line | area | bar | pie | donut. Lines draw themselves in, bars grow,
// pies sweep open. Hover shows the values under the pointer. Bars with a single
// series go green above zero and red below (P&L).
(() => {
  const TAU = Math.PI * 2;
  const PALETTE = [[92, 200, 255], [255, 196, 92], [79, 227, 161], [190, 140, 255], [255, 120, 160],
                   [120, 235, 230], [240, 240, 140], [160, 200, 255]];
  const UP = [79, 227, 161], DOWN = [255, 107, 107];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const FONT = "ui-monospace, Consolas, monospace";
  const ease = (t) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

  function fmt(v, unit) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    const a = Math.abs(v);
    if (a === 0) return unit === "$" || unit === "%" ? (unit === "$" ? "$0" : "0%") : "0";
    const s = a >= 1e9 ? (a / 1e9).toFixed(1) + "B" : a >= 1e6 ? (a / 1e6).toFixed(1) + "M"
      : a >= 1e4 ? (a / 1e3).toFixed(1) + "k" : a >= 100 ? a.toFixed(0) : a >= 1 ? a.toFixed(2).replace(/\.?0+$/, "") : a.toPrecision(2);
    const sign = v < 0 ? "-" : "";
    if (unit === "$" || unit === "€" || unit === "£" || unit === "₹") return sign + unit + s;
    return sign + s + (unit ? (unit === "%" ? "%" : " " + unit) : "");
  }
  function niceTicks(lo, hi, n = 5) {
    if (lo === hi) { lo -= 1; hi += 1; }
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    const start = Math.floor(lo / step) * step, ticks = [];
    for (let v = start; v <= hi + step * 0.5; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }

  class JarvisChart {
    constructor(canvas, spec) {
      this.cv = canvas; this.g = canvas.getContext("2d"); this.s = spec;
      this.born = performance.now(); this.hover = null;
      canvas.addEventListener("pointermove", (e) => {
        const r = canvas.getBoundingClientRect(); this.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      });
      canvas.addEventListener("pointerleave", () => { this.hover = null; });
      const loop = (now) => { if (!canvas.isConnected) return; this.draw(now); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    draw(now) {
      const cv = this.cv, dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
      if (!W || !H) return;
      if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
      const g = this.g; g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
      const p = ease((now - this.born) / 900);
      const k = this.s.kind;
      if (k === "pie" || k === "donut") this.pie(g, W, H, p);
      else this.xy(g, W, H, p, now);
    }

    legend(g, names, cols, W) {
      g.font = `10px ${FONT}`; g.textBaseline = "middle"; g.textAlign = "left";
      let x = 8;
      names.forEach((n, i) => {
        if (!n) return;
        g.fillStyle = rgba(cols[i], 0.95); g.fillRect(x, 6, 10, 3);
        g.fillStyle = "rgba(170,200,225,.9)"; g.fillText(n.toUpperCase(), x + 14, 8);
        x += 22 + g.measureText(n.toUpperCase()).width;
        if (x > W - 60) x = 8;
      });
    }

    xy(g, W, H, p, now) {
      const { x, series, unit, stacked } = this.s, kind = this.s.kind, n = x.length;
      const multi = series.length > 1;
      const cols = series.map((_, i) => PALETTE[i % PALETTE.length]);
      // value range (stacked sums when asked)
      let lo = Infinity, hi = -Infinity;
      const tops = Array(n).fill(0), bots = Array(n).fill(0);
      for (let i = 0; i < n; i++) for (const s of series) {
        const v = s.values[i]; if (v === null || v === undefined) continue;
        if (stacked && kind !== "line") { if (v >= 0) tops[i] += v; else bots[i] += v; lo = Math.min(lo, bots[i]); hi = Math.max(hi, tops[i]); }
        else { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
      if (kind !== "line") { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      const ticks = niceTicks(lo, hi); lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
      g.font = `10px ${FONT}`;
      const padL = Math.max(...ticks.map((t) => g.measureText(fmt(t, unit)).width)) + 12, padR = 10, padT = multi ? 22 : 10, padB = 24;
      const X0 = padL, X1 = W - padR, Y0 = padT, Y1 = H - padB;
      const yOf = (v) => Y1 - ((v - lo) / (hi - lo)) * (Y1 - Y0);
      const band = (X1 - X0) / Math.max(1, n), xOf = (i) => (kind === "bar" ? X0 + band * (i + 0.5) : X0 + (n > 1 ? (i / (n - 1)) * (X1 - X0) : (X1 - X0) / 2));

      // grid + axis labels
      g.textAlign = "right"; g.textBaseline = "middle";
      for (const t of ticks) {
        const y = yOf(t);
        g.strokeStyle = t === 0 ? "rgba(120,190,240,.45)" : "rgba(80,190,255,.1)"; g.lineWidth = 1;
        g.beginPath(); g.moveTo(X0, Math.round(y) + 0.5); g.lineTo(X1, Math.round(y) + 0.5); g.stroke();
        g.fillStyle = "rgba(120,160,190,.9)"; g.fillText(fmt(t, unit), X0 - 6, y);
      }
      g.textAlign = "center"; g.textBaseline = "top";
      const every = Math.max(1, Math.ceil((n * 58) / (X1 - X0)));
      for (let i = 0; i < n; i += every) {
        const lx = xOf(i);  // keep the end labels inside the chart
        g.textAlign = kind !== "bar" && i === 0 ? "left" : kind !== "bar" && i >= n - every && lx > X1 - 30 ? "right" : "center";
        g.fillStyle = "rgba(120,160,190,.9)"; g.fillText(x[i].slice(0, 12), lx, Y1 + 6);
      }

      // hover index
      let hi_ = null;
      if (this.hover && this.hover.x >= X0 && this.hover.x <= X1 && this.hover.y >= Y0 && this.hover.y <= Y1) {
        hi_ = kind === "bar" ? Math.floor((this.hover.x - X0) / band) : Math.round(((this.hover.x - X0) / (X1 - X0)) * (n - 1));
        hi_ = Math.max(0, Math.min(n - 1, hi_));
      }

      g.globalCompositeOperation = "lighter";
      if (kind === "bar") {
        const groups = stacked ? 1 : series.length, bw = Math.max(2, (band * 0.72) / groups);
        const up = Array(n).fill(0), dn = Array(n).fill(0);
        series.forEach((s, si) => {
          for (let i = 0; i < n; i++) {
            const v = s.values[i]; if (v === null || v === undefined) continue;
            let base = 0, end = v;
            if (stacked) { if (v >= 0) { base = up[i]; up[i] += v; end = up[i]; } else { base = dn[i]; dn[i] += v; end = dn[i]; } }
            const bx = stacked ? xOf(i) - bw / 2 : xOf(i) - (band * 0.72) / 2 + si * bw;
            const yb = yOf(base), ye = yb + (yOf(end) - yb) * p;
            const c = multi ? cols[si] : v >= 0 ? UP : DOWN;
            const grd = g.createLinearGradient(0, ye, 0, yb);
            grd.addColorStop(0, rgba(c, hi_ === i ? 1 : 0.85)); grd.addColorStop(1, rgba(c, 0.25));
            g.fillStyle = grd; g.fillRect(bx + 1, Math.min(yb, ye), bw - 2, Math.abs(ye - yb));
            g.fillStyle = rgba(c, 1); g.fillRect(bx + 1, ye - (v >= 0 ? 1 : 0), bw - 2, 1.5);
          }
        });
      } else {
        const acc = Array(n).fill(0);
        series.forEach((s, si) => {
          const c = cols[si], pts = [];
          for (let i = 0; i < n; i++) {
            const v = s.values[i]; if (v === null || v === undefined) { pts.push(null); continue; }
            const base = stacked && kind === "area" ? acc[i] : 0;
            if (stacked && kind === "area") acc[i] += v;
            pts.push([xOf(i), yOf(base + v), yOf(base)]);
          }
          const cut = X0 + (X1 - X0) * p;  // draws in from the left
          g.save(); g.beginPath(); g.rect(0, 0, cut, H); g.clip();
          if (kind === "area") {
            const grd = g.createLinearGradient(0, Y0, 0, Y1); grd.addColorStop(0, rgba(c, 0.35)); grd.addColorStop(1, rgba(c, 0.02));
            g.fillStyle = grd; g.beginPath(); let started = false, lastB = [];
            pts.forEach((q) => { if (!q) return; if (!started) { g.moveTo(q[0], q[2]); started = true; } g.lineTo(q[0], q[1]); lastB.push(q); });
            for (let j = lastB.length - 1; j >= 0; j--) g.lineTo(lastB[j][0], lastB[j][2]);
            g.closePath(); g.fill();
          }
          g.strokeStyle = rgba(c, 0.95); g.lineWidth = 2; g.shadowColor = rgba(c, 1); g.shadowBlur = 8;
          g.beginPath(); let pen = false;
          pts.forEach((q) => { if (!q) { pen = false; return; } if (!pen) { g.moveTo(q[0], q[1]); pen = true; } else g.lineTo(q[0], q[1]); });
          g.stroke(); g.shadowBlur = 0;
          if (n <= 40) for (const q of pts) if (q) { g.fillStyle = rgba(c, 0.9); g.beginPath(); g.arc(q[0], q[1], 2, 0, TAU); g.fill(); }
          // a spark riding the line's leading edge while it draws in
          if (p < 1) { const q = [...pts].reverse().find((q) => q && q[0] <= cut); if (q) { g.fillStyle = "#fff"; g.beginPath(); g.arc(q[0], q[1], 3, 0, TAU); g.fill(); } }
          g.restore();
        });
      }
      g.globalCompositeOperation = "source-over";
      if (multi) this.legend(g, series.map((s) => s.name), cols, W);

      if (hi_ !== null && p >= 1) {  // crosshair + values
        const hx = xOf(hi_);
        g.strokeStyle = "rgba(200,235,255,.35)"; g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(hx, Y0); g.lineTo(hx, Y1); g.stroke(); g.setLineDash([]);
        const lines = [x[hi_], ...series.map((s) => (s.name ? s.name + ": " : "") + fmt(s.values[hi_], unit))];
        g.font = `11px ${FONT}`; g.textAlign = "left"; g.textBaseline = "top";
        const w = Math.max(...lines.map((l) => g.measureText(l).width)) + 16, h = lines.length * 15 + 8;
        let bx = hx + 10; if (bx + w > W) bx = hx - w - 10;
        g.fillStyle = "rgba(3,14,30,.94)"; g.fillRect(bx, Y0, w, h);
        g.strokeStyle = "rgba(110,210,255,.6)"; g.strokeRect(bx + 0.5, Y0 + 0.5, w - 1, h - 1);
        lines.forEach((l, i) => { g.fillStyle = i ? rgba(multi ? cols[i - 1] : [207, 233, 255], 1) : "#8fb3cf"; g.fillText(l, bx + 8, Y0 + 5 + i * 15); });
      }
    }

    pie(g, W, H, p) {
      const { x, series, unit, kind } = this.s, vals = series[0].values.map((v) => Math.max(0, v || 0));
      const total = vals.reduce((a, b) => a + b, 0) || 1;
      const legendW = Math.min(220, W * 0.42), cx = (W - legendW) / 2, cy = H / 2, R = Math.max(10, Math.min(cx, cy) - 12);
      const r0 = kind === "donut" ? R * 0.58 : 0;
      let hoverI = null;
      if (this.hover) {
        const dx = this.hover.x - cx, dy = this.hover.y - cy, d = Math.hypot(dx, dy);
        if (d <= R + 6 && d >= r0) {
          let a = Math.atan2(dy, dx) + Math.PI / 2; if (a < 0) a += TAU;
          let acc = 0; vals.forEach((v, i) => { const s = (v / total) * TAU; if (a >= acc && a < acc + s) hoverI = i; acc += s; });
        }
      }
      let a = -Math.PI / 2;
      g.globalCompositeOperation = "lighter";
      vals.forEach((v, i) => {
        const sweep = (v / total) * TAU * p, c = PALETTE[i % PALETTE.length], out = hoverI === i ? 6 : 0;
        const mid = a + sweep / 2, ox = Math.cos(mid) * out, oy = Math.sin(mid) * out;
        g.fillStyle = rgba(c, hoverI === null || hoverI === i ? 0.85 : 0.35);
        g.beginPath(); g.moveTo(cx + ox + Math.cos(a) * r0, cy + oy + Math.sin(a) * r0);
        g.arc(cx + ox, cy + oy, R, a, a + sweep); g.arc(cx + ox, cy + oy, r0, a + sweep, a, true); g.closePath(); g.fill();
        g.strokeStyle = "rgba(2,10,24,.9)"; g.lineWidth = 2; g.stroke();
        a += sweep;
      });
      g.globalCompositeOperation = "source-over";
      if (kind === "donut") {
        const show = hoverI ?? null;
        g.textAlign = "center"; g.textBaseline = "middle";
        g.fillStyle = "#bfe9ff"; g.font = `${Math.max(12, R * 0.2)}px ${FONT}`;
        g.fillText(fmt(show === null ? total : vals[show], unit), cx, cy - 6);
        g.fillStyle = "#5f86a6"; g.font = `10px ${FONT}`;
        g.fillText(show === null ? "TOTAL" : (x[show] || "").toUpperCase().slice(0, 18), cx, cy + R * 0.16);
      }
      g.font = `11px ${FONT}`; g.textAlign = "left"; g.textBaseline = "middle";
      const lx = W - legendW + 8, rowH = Math.min(20, (H - 10) / Math.max(1, vals.length));
      vals.forEach((v, i) => {
        const y = 10 + rowH * i + rowH / 2, c = PALETTE[i % PALETTE.length];
        g.fillStyle = rgba(c, hoverI === null || hoverI === i ? 1 : 0.4); g.fillRect(lx, y - 4, 8, 8);
        g.fillStyle = hoverI === i ? "#ffffff" : "rgba(200,230,250,.9)";
        g.fillText(`${(x[i] || "").slice(0, 16)}  ${((v / total) * 100).toFixed(1)}%`, lx + 14, y);
      });
    }
  }
  window.JarvisChart = JarvisChart;
})();
