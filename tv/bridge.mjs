// Jarvis's helper for TradingView Desktop, on tradingview-mcp-main's stack (its CDP client,
// chrome-remote-interface, and its launch/health code).
//
// It works on ONE chart: the chart showing the zones symbol (NASDAQ:QQQ) in ONE layout
// (default "Main"). It never changes any chart's symbol and never touches other layouts.
// (The bridge's own core functions always act on the first chart tab and its active chart,
// so the targeting is done here.)
//
//   node tv/bridge.mjs <command> '<json args>'   → one JSON object on stdout
//
//   status      {layout, symbol}                   layouts/charts open, and the target chart
//   read        {layout, symbol, timeframe, count} bars from the target chart (switching only its timeframe)
//   restore     {layout, symbol, timeframe}        put the target chart's timeframe back
//   draw_zones  {layout, symbol, zones, mine_re, colors}   replace Jarvis's zone boxes on it
//   draw_level  {layout, symbol, price, text, color}       a labelled horizontal line on it
//   clear_mine  {layout, mine_re}                  remove Jarvis's drawings (matching text) in the layout
//
// It never removes drawings whose text doesn't match the given pattern.

const BRIDGE = (process.env.JARVIS_TV_MCP || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/tradingview-mcp-main').split('\\').join('/');
const { createRequire } = await import('node:module');
const CDP = createRequire(`${BRIDGE}/package.json`)('chrome-remote-interface');
const PORT = 9222, HOST = '127.0.0.1';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clients = [];

async function chartTabs() {
  const r = await fetch(`http://${HOST}:${PORT}/json/list`, { signal: AbortSignal.timeout(3000) });
  return (await r.json()).filter((t) => t.type === 'page' && /^https:\/\/([a-z]+\.)?tradingview\.com\/chart\//.test(t.url));
}

async function open(tab) {
  const c = await CDP({ host: HOST, port: PORT, target: tab.id });
  clients.push(c);
  await c.Runtime.enable();
  return c;
}

/** Run a self-contained function in the page with one JSON argument; promises are awaited. */
async function inPage(client, fn, arg) {
  const r = await client.Runtime.evaluate({ expression: `(${fn.toString()})(${JSON.stringify(arg ?? null)})`,
                                             awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(String(r.exceptionDetails.exception?.description || r.exceptionDetails.text).split('\n')[0]);
  return r.result.value;
}

// ---- functions that run inside the TradingView page ----

function pageInfo() {
  const a = window.TradingViewApi, charts = [];
  for (let i = 0; i < a.chartsCount(); i++) { try { charts.push({ i, symbol: a.chart(i).symbol(), tf: a.chart(i).resolution() }); } catch {} }
  let layout = null, active = 0;
  try { layout = a.layoutName ? a.layoutName() : null; } catch {}
  try { active = a.activeChartIndex ? a.activeChartIndex() : 0; } catch {}
  return { layout, active, charts };
}

async function pageRead({ chart, timeframe, count }) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const c = TradingViewApi.chart(chart);
  const was = c.resolution();
  if (timeframe && was !== timeframe) await new Promise((r) => { c.setResolution(timeframe, r); setTimeout(r, 15000); });
  const s = c._chartWidget.model().mainSeries();
  const loading = () => { try { const v = s.isLoading(); return v && typeof v === 'object' && 'value' in v ? !!v.value() : !!v; } catch { return false; } };
  const spacing = () => {
    const l = s.bars(), gaps = {};
    for (let i = Math.max(l.firstIndex() + 1, l.lastIndex() - 60); i <= l.lastIndex(); i++) {
      const x = l.valueAt(i - 1), y = l.valueAt(i); if (x && y) gaps[y[0] - x[0]] = (gaps[y[0] - x[0]] || 0) + 1;
    }
    return Number(Object.entries(gaps).sort((p, q) => q[1] - p[1])[0]?.[0] ?? 0);
  };
  for (let i = 0; i < 60 && (loading() || s.bars().size() === 0); i++) await sleep(250);
  if (timeframe) for (let i = 0; i < 80 && spacing() !== Number(timeframe) * 60; i++) await sleep(250);
  for (let k = 0; k < 6 && s.bars().size() < count; k++) {  // ask for older bars until there are `count`
    const before = s.bars().size();
    try { s.requestMoreData(count - before); } catch { break; }
    for (let i = 0; i < 40; i++) { await sleep(250); if (s.bars().size() > before && !loading()) break; }
    if (s.bars().size() === before) break;
  }
  const l = s.bars(), bars = [];
  for (let i = l.firstIndex(); i <= l.lastIndex(); i++) { const v = l.valueAt(i); if (v) bars.push(v.slice(0, 6)); }
  return { symbol: c.symbol(), timeframe: c.resolution(), was, bars: bars.slice(-count), spacing: spacing() };
}

async function pageSetTimeframe({ chart, timeframe }) {
  const c = TradingViewApi.chart(chart);
  if (timeframe && c.resolution() !== timeframe) await new Promise((r) => { c.setResolution(timeframe, r); setTimeout(r, 15000); });
  return c.resolution();
}

function pageRemoveMine({ chart, mineRe }) {
  const a = TradingViewApi, re = new RegExp(mineRe);
  let removed = 0;
  const charts = chart === null ? [...Array(a.chartsCount()).keys()] : [chart];
  for (const i of charts) {
    let c; try { c = a.chart(i); } catch { continue; }
    for (const s of c.getAllShapes()) {
      let t = ''; try { t = c.getShapeById(s.id).getProperties().text || ''; } catch {}
      if (re.test(t)) { try { c.removeEntity(s.id); removed++; } catch {} }
    }
  }
  return removed;
}

async function pageDrawZones({ chart, zones, mineRe, colors }) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const c = TradingViewApi.chart(chart), re = new RegExp(mineRe);
  const sweep = (keep) => {
    let n = 0;
    for (const s of c.getAllShapes()) {
      if (keep.includes(s.id)) continue;
      let t = ''; try { t = c.getShapeById(s.id).getProperties().text || ''; } catch {}
      if (re.test(t)) { try { c.removeEntity(s.id); n++; } catch {} }
    }
    return n;
  };
  // a just-opened chart loads its saved drawings a moment later: wait until the count settles (~8 s max)
  for (let i = 0, last = -1, same = 0; i < 16 && same < 3; i++) { const n = c.getAllShapes().length; same = n === last ? same + 1 : 0; last = n; await wait(500); }
  let removed = sweep([]);
  const ids = [];
  for (const z of zones) {
    const rgb = z.side === 'resistance' ? colors.resistance : colors.support;
    const make = () => c.createMultipointShape([{ time: z.from, price: z.hi }, { time: z.to, price: z.lo }], {
      shape: 'rectangle', lock: true, text: z.label,
      overrides: { backgroundColor: `rgba(${rgb},${colors.fill})`, color: `rgba(${rgb},${colors.border})`, linewidth: 1, extendRight: true,
                   textColor: `rgb(${rgb})`, fontSize: 11, horzLabelsAlign: 'right', vertLabelsAlign: 'middle' },
    });
    for (let i = 0; ; i++) { try { ids.push(await make()); break; } catch (e) { if (i >= 40) throw e; await wait(500); } }
  }
  for (let i = 0; i < 3; i++) { await wait(800); removed += sweep(ids); }  // old boxes that loaded late
  return { removed, drawn: ids.length };
}

async function pageDrawLevel({ chart, price, text, color }) {
  const c = TradingViewApi.chart(chart);
  return c.createShape({ time: Math.floor(Date.now() / 1000), price }, {
    shape: 'horizontal_line', lock: true, text,
    overrides: { linecolor: `rgb(${color})`, linewidth: 1, linestyle: 2, showLabel: true, textcolor: `rgb(${color})`, horzLabelsAlign: 'right' },
  });
}

// ---- choosing the layout and the chart ----

const ticker = (s) => String(s || '').split(':').pop().toUpperCase();

/** The layout tab: by name (exact, case-insensitive, then prefix) or by the id in its URL. */
async function findLayout(layout) {
  const tabs = await chartTabs();
  if (!tabs.length) throw new Error('no TradingView chart tab is open');
  const seen = [];
  for (const t of tabs) {
    const client = await open(t);
    let info; try { info = await inPage(client, pageInfo); } catch { continue; }
    seen.push({ tab: t, client, info });
  }
  const want = String(layout || '').trim().toLowerCase();
  const nm = (x) => String(x.info.layout || '').trim().toLowerCase();
  const hit = !want ? seen[0]
    : seen.find((x) => nm(x) === want) || seen.find((x) => nm(x).startsWith(want)) || seen.find((x) => x.tab.url.includes(`/chart/${layout}/`));
  if (!hit) throw new Error(`the TradingView layout "${layout}" isn't open (open: ${seen.map((x) => x.info.layout).filter(Boolean).join(', ') || 'none'})`);
  return hit;
}

/** The chart in that layout showing `symbol` itself (not a future or an option on it); active one first. */
function findChart(info, symbol) {
  const want = String(symbol), wantT = ticker(symbol);
  const exact = info.charts.filter((c) => c.symbol === want);
  const same = exact.length ? exact : info.charts.filter((c) => ticker(c.symbol) === wantT);
  if (!same.length) {
    throw new Error(`no chart shows ${symbol} in layout "${info.layout}" (charts: ${info.charts.map((c) => c.symbol).join(', ')}); I won't change a chart's symbol`);
  }
  return (same.find((c) => c.i === info.active) || same[0]).i;
}

const commands = {
  async status({ layout, symbol }) {
    const tabs = await chartTabs();
    const { client, info } = await findLayout(layout);
    let chart = null; try { chart = findChart(info, symbol); } catch {}
    const target = chart === null ? null : info.charts.find((c) => c.i === chart);
    void client;
    return { layouts: tabs.length, layout: info.layout, charts: info.charts, chart, symbol: target?.symbol, timeframe: target?.tf };
  },

  async read({ layout, symbol, timeframe, count = 500 }) {
    const { client, info } = await findLayout(layout);
    const chart = findChart(info, symbol);
    const r = await inPage(client, pageRead, { chart, timeframe: timeframe ? String(timeframe) : null, count });
    const back = { timeframe: r.was };
    if (timeframe && r.spacing !== Number(timeframe) * 60) {
      try { await inPage(client, pageSetTimeframe, { chart, timeframe: r.was }); } catch {}
      throw new Error(`the chart's bars are ${r.spacing / 60}-min, not ${timeframe}-min yet`);
    }
    if (r.bars.length < 30) throw new Error(`only ${r.bars.length} bars on the chart`);
    return { layout: info.layout, chart, symbol: r.symbol, timeframe: r.timeframe, before: back, bars: r.bars };
  },

  async restore({ layout, symbol, timeframe }) {
    const { client, info } = await findLayout(layout);
    const chart = findChart(info, symbol);
    return { timeframe: await inPage(client, pageSetTimeframe, { chart, timeframe: timeframe ? String(timeframe) : null }) };
  },

  async draw_zones({ layout, symbol, zones, mine_re, colors }) {
    const { client, info } = await findLayout(layout);
    const chart = findChart(info, symbol);
    const r = await inPage(client, pageDrawZones, { chart, zones, mineRe: mine_re, colors });
    // old Jarvis zone boxes on the layout's other charts (only ones labelled like "R1 671.20–671.80")
    let elsewhere = 0;
    for (const c of info.charts) if (c.i !== chart) {
      elsewhere += await inPage(client, pageRemoveMine, { chart: c.i, mineRe: mine_re });
    }
    return { layout: info.layout, chart, removed: r.removed + elsewhere, drawn: r.drawn };
  },

  async draw_level({ layout, symbol, price, text, color = '255,181,71' }) {
    const { client, info } = await findLayout(layout);
    const chart = findChart(info, symbol);
    return { layout: info.layout, chart, id: await inPage(client, pageDrawLevel, { chart, price, text, color }) };
  },

  async clear_mine({ layout, mine_re }) {
    const { client, info } = await findLayout(layout);
    return { layout: info.layout, removed: await inPage(client, pageRemoveMine, { chart: null, mineRe: mine_re }) };
  },
};

const [cmd, raw] = process.argv.slice(2);
try {
  if (!commands[cmd]) throw new Error(`unknown command ${cmd}`);
  process.stdout.write(JSON.stringify({ ok: true, ...(await commands[cmd](raw ? JSON.parse(raw) : {})) }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e?.message || e).split('\n')[0].slice(0, 300) }));
} finally {
  for (const c of clients) { try { await c.close(); } catch {} }
  process.exit(0);
}
