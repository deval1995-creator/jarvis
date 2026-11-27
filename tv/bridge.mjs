// Jarvis's helper for TradingView Desktop, built on tradingview-mcp-main's own library
// (its src/core and connection.js), so Jarvis and that project share one implementation.
//
//   node tv/bridge.mjs <command> '<json args>'   → one JSON object on stdout
//
// Commands:
//   status                              is TradingView reachable (and on which chart)
//   launch                              start TradingView with its debug port
//   read   {symbol, timeframe, count}   switch the chart, wait until its bars really are that
//                                       timeframe, return bars + quote + what it was before
//   restore {symbol?, timeframe}        put the chart back
//   draw_zones {zones, mine_re, colors} remove Jarvis's old zone boxes, draw the new ones
//   draw_level {price, text, color}     a labelled horizontal line ("J· …", so it's ours)
//   clear_mine {mine_re}                remove only shapes whose text matches mine_re
//
// It never touches drawings whose text doesn't match the given pattern.

const BRIDGE = (process.env.JARVIS_TV_MCP || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/tradingview-mcp-main').replace(/\\/g, '/');
const core = await import(`file:///${BRIDGE}/src/core/index.js`);
const conn = await import(`file:///${BRIDGE}/src/connection.js`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = (obj) => { process.stdout.write(JSON.stringify(obj)); };

async function api() { return conn.getChartApi(); }

/** Most common gap between the last bars, in seconds (right after a timeframe switch the old bars linger). */
function spacing(bars) {
  const gaps = {};
  for (let i = Math.max(1, bars.length - 60); i < bars.length; i++) {
    const g = bars[i].time - bars[i - 1].time; gaps[g] = (gaps[g] || 0) + 1;
  }
  return Number(Object.entries(gaps).sort((a, b) => b[1] - a[1])[0]?.[0] ?? 0);
}

async function shapesWithText() {
  const a = await api();
  return conn.evaluate(`(function(){ var api = ${a}; return api.getAllShapes().map(function(s){
    var t = ''; try { t = api.getShapeById(s.id).getProperties().text || ''; } catch (e) {}
    return { id: s.id, name: s.name, text: t }; }); })()`);
}

async function removeMatching(mineRe) {
  const re = new RegExp(mineRe);
  const mine = (await shapesWithText()).filter((s) => re.test(s.text));
  // (the bridge's own removeOne/listDrawings reference undefined names, so remove directly)
  const a = await api();
  for (const s of mine) {
    try { await conn.evaluate(`(function(){ ${a}.removeEntity(${JSON.stringify(s.id)}); })()`); } catch {}
  }
  return mine.length;
}

const commands = {
  async status() {
    const h = await core.health.healthCheck();
    let state = null;
    try { state = await core.chart.getState(); } catch {}
    return { ok: true, health: h, symbol: state?.symbol, timeframe: state?.resolution };
  },

  async launch() { return core.health.launch({}); },

  async read({ symbol, timeframe, count = 500 }) {
    const before = await core.chart.getState();
    const changed = [];
    if (symbol && before.symbol.split(':').pop() !== symbol.split(':').pop()) {
      await core.chart.setSymbol({ symbol }); changed.push('symbol');
    }
    if (timeframe && String(before.resolution) !== String(timeframe)) {
      await core.chart.setTimeframe({ timeframe: String(timeframe) }); changed.push('timeframe');
    }
    // never read bars of the wrong timeframe: wait until they are `timeframe` minutes apart (~20 s)
    let bars = [], gap = 0;
    for (let i = 0; i < 40; i++) {
      try { bars = (await core.data.getOhlcv({ count })).bars; gap = spacing(bars); } catch { bars = []; }
      if (bars.length && (!timeframe || gap === Number(timeframe) * 60)) break;
      await sleep(500);
    }
    if (!bars.length) throw new Error('the chart has no bars yet (still loading?)');
    if (timeframe && gap !== Number(timeframe) * 60) throw new Error(`the chart's bars are ${gap / 60}-min, not ${timeframe}-min`);
    let quote = null;
    try { quote = await core.data.getQuote({}); } catch {}
    const now = await core.chart.getState();
    return { symbol: now.symbol, timeframe: now.resolution, before: { symbol: before.symbol, timeframe: before.resolution },
             changed, bars: bars.map((b) => [b.time, b.open, b.high, b.low, b.close, b.volume]), quote };
  },

  async restore({ symbol, timeframe }) {
    const now = await core.chart.getState();
    if (symbol && now.symbol !== symbol) await core.chart.setSymbol({ symbol });
    if (timeframe && String(now.resolution) !== String(timeframe)) await core.chart.setTimeframe({ timeframe: String(timeframe) });
    return { restored: true };
  },

  async draw_zones({ zones, mine_re, colors }) {
    // a just-opened chart loads its saved drawings a moment later: wait until the count settles (~5 s)
    for (let i = 0, last = -1, same = 0; i < 10 && same < 2; i++) {
      const n = (await shapesWithText()).length; same = n === last ? same + 1 : 0; last = n; await sleep(500);
    }
    const removed = await removeMatching(mine_re);
    const ids = [];
    for (const z of zones) {
      const rgb = z.side === 'resistance' ? colors.resistance : colors.support;
      const r = await core.drawing.drawShape({
        shape: 'rectangle', point: { time: z.from, price: z.hi }, point2: { time: z.to, price: z.lo }, text: z.label,
        overrides: { backgroundColor: `rgba(${rgb},${colors.fill})`, color: `rgba(${rgb},${colors.border})`, linewidth: 1,
                     extendRight: true, textColor: `rgb(${rgb})`, fontSize: 11, horzLabelsAlign: 'right', vertLabelsAlign: 'middle',
                     showLabel: true },
      });
      ids.push(r.entity_id);
    }
    return { removed, drawn: ids.filter(Boolean).length, ids };
  },

  async draw_level({ price, text, color = '255,181,71' }) {
    const r = await core.drawing.drawShape({
      shape: 'horizontal_line', point: { time: Math.floor(Date.now() / 1000), price },
      overrides: { linecolor: `rgb(${color})`, linewidth: 1, linestyle: 2, showLabel: true, textcolor: `rgb(${color})`,
                   horzLabelsAlign: 'right' }, text,
    });
    return { id: r.entity_id };
  },

  async clear_mine({ mine_re }) { return { removed: await removeMatching(mine_re) }; },
};

const [cmd, raw] = process.argv.slice(2);
try {
  if (!commands[cmd]) throw new Error(`unknown command ${cmd}`);
  out({ ok: true, ...(await commands[cmd](raw ? JSON.parse(raw) : {})) });
} catch (e) {
  out({ ok: false, error: String(e?.message || e).split('\n')[0].slice(0, 300) });
} finally {
  try { await conn.disconnect(); } catch {}
  process.exit(0);
}
