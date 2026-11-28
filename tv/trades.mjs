// Jarvis's trade watcher: reads your broker positions and orders from TradingView Desktop.
//
// TradingView keeps its broker connection (Interactive Brokers) in the page, whether or not
// the trading panel is open. This listens to TradingView's own live feeds: the position
// service (fires on every P&L tick from the broker) and the quote stream for each open
// contract (the same last price TradingView shows). Any change is pushed out within ~150 ms;
// it also reads every 1 s as a backstop (every 300 ms while in a trade). One JSON line goes to
// stdout per change, plus a heartbeat every 20 s. READ-ONLY: it never places, changes or
// cancels anything, and never clicks in TradingView.
//
//   node tv/trades.mjs            watch (the Core runs this and reads the lines)
//   node tv/trades.mjs once       one snapshot, then exit

const BRIDGE = (process.env.JARVIS_TV_MCP || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/tradingview-mcp-main').split('\\').join('/');
const { createRequire } = await import('node:module');
const CDP = createRequire(`${BRIDGE}/package.json`)('chrome-remote-interface');
const HOST = '127.0.0.1', PORT = Number(process.env.JARVIS_TV_PORT || 9222);
const IDLE_MS = 1000, IN_TRADE_MS = 300, HEARTBEAT_MS = 20000, RETRY_MS = 10000;
const BINDING = '__jarvisTradesPush';

// Runs inside TradingView: hooks TradingView's live feeds (idempotent). Listeners only record
// quotes and ping us (throttled); they never act on the account.
const LISTEN = `(() => {
  const api = window.TradingViewApi;
  const t = api && typeof api.trading === 'function' ? api.trading() : null;
  const ab = t && t._activeBroker;
  const b = ab && typeof ab.value === 'function' ? ab.value() : ab;
  const svc = t && t._positionService;
  if (!b || !svc) return false;
  const J = window.__jarvisTrades || (window.__jarvisTrades = { quotes: {}, subs: new Map(), timer: 0 });
  J.ping = () => { if (J.timer) return; J.timer = setTimeout(() => { J.timer = 0; try { window.${BINDING} && window.${BINDING}('1'); } catch {} }, 120); };
  if (J.svc !== svc) {
    try { J.svc && J.svc.positionUpdate().unsubscribe(J, J.onPos); J.svc && J.svc.positionsRemoved().unsubscribe(J, J.onPos); } catch {}
    J.onPos = () => J.ping();
    svc.positionUpdate().subscribe(J, J.onPos);
    svc.positionsRemoved().subscribe(J, J.onPos);
    J.svc = svc;
  }
  if (J.broker !== b) { J.subs.clear(); J.quotes = {}; J.broker = b; }
  return true;
})()`;

// Runs inside TradingView. Returns plain data only.
const SNAPSHOT = `(async () => {
  const api = window.TradingViewApi;
  const t = api && typeof api.trading === 'function' ? api.trading() : null;
  if (!t) return { status: 'loading' };
  const ab = t._activeBroker;
  const b = ab && typeof ab.value === 'function' ? ab.value() : ab;
  if (!b) return { status: 'no_broker' };
  let meta = {}; try { meta = b.metainfo() || {}; } catch {}
  const broker = meta.title || meta.id || 'broker';
  const st = b.connectionStatus();
  if (st !== 1) return { status: st === 2 ? 'connecting' : 'disconnected', broker };
  const num = (x) => (typeof x === 'number' && isFinite(x) ? x : null);
  const [positions, orders] = await Promise.all([b.positions(), b.orders()]);
  const svc = t._positionService;
  const J = window.__jarvisTrades;
  const live = (sym) => {  // the quote TradingView streams for this contract: last trade, else mid
    const q = J && J.quotes[sym];
    if (!q) return null;
    if (num(q.trade) !== null) return q.trade;
    return num(q.bid) !== null && num(q.ask) !== null ? +((q.bid + q.ask) / 2).toFixed(4) : null;
  };
  const open = (positions || []).filter((p) => p && p.qty).map((p) => {
    let pl = null;
    try { pl = num(svc.find(String(p.id))?.pl); } catch {}  // the P&L TradingView's own panel shows, live from the broker
    if (pl === null) pl = num(p.pl);
    return { id: String(p.id), symbol: p.symbol, name: p.symbolDisplayName || p.symbol, qty: Math.abs(p.qty),
             side: p.side === -1 ? 'short' : 'long', avg: num(p.avgPrice), pl, last: live(p.symbol) ?? num(p.last) };
  });
  if (J) {  // stream quotes for open contracts only
    const want = new Set(open.map((p) => p.symbol));
    for (const sym of want) if (!J.subs.has(sym)) {
      const fn = (s, d) => { J.quotes[sym] = Object.assign(J.quotes[sym] || {}, d); J.ping(); };
      J.subs.set(sym, fn);
      try { b.subscribeRealtime(sym, fn); } catch {}
    }
    for (const [sym, fn] of [...J.subs]) if (!want.has(sym)) {
      try { b.unsubscribeRealtime(sym, fn); } catch {}
      J.subs.delete(sym); delete J.quotes[sym];
    }
  }
  const TYPE = { 1: 'limit', 2: 'market', 3: 'stop', 4: 'stop_limit' };
  const o = (orders || []).map((x) => ({
    id: String(x.id), symbol: x.symbol, name: x.symbolDisplayName || x.symbol, type: TYPE[x.type] || String(x.type),
    side: x.side === -1 ? 'sell' : 'buy', qty: num(x.qty), filled: num(x.filledQty), status: x.status,
    limit: num(x.limitPrice), stop: num(x.stopPrice), avg: num(x.avgPrice), parent: x.parentId ? String(x.parentId) : null,
    time: num(x.extra && x.extra.lastExecutionTime) }));
  const summary = {};
  try {
    for (const s of b.accountManagerInfo().summary || []) {
      const v = s.wValue && typeof s.wValue.value === 'function' ? s.wValue.value() : null;
      if (/daily/i.test(s.text)) summary.daily_pnl = num(v);
      else if (/unrealized/i.test(s.text)) summary.unrealized_pnl = num(v);
      else if (/net liq/i.test(s.text)) summary.net_liq = num(v);
    }
  } catch {}
  let account = null; try { account = b.currentAccount(); } catch {}
  return { status: 'ok', broker, account, positions: open,
           working: o.filter((x) => x.status === 6 || x.status === 4),   // working or being placed
           fills: o.filter((x) => x.status === 2 && x.time).sort((a, z) => a.time - z.time), ...summary };
})()`;

const out = (obj) => process.stdout.write(JSON.stringify({ at: Date.now(), ...obj }) + '\n');

async function connect() {
  const tabs = (await CDP.List({ host: HOST, port: PORT })).filter((t) => t.type === 'page' && /tradingview\.com\/chart/.test(t.url));
  if (!tabs.length) throw Object.assign(new Error('no chart tab'), { code: 'no_chart' });
  return CDP({ host: HOST, port: PORT, target: tabs[0].id });
}

async function listen(c, onPush) {
  try { await c.Runtime.addBinding({ name: BINDING }); } catch {}
  c.on('Runtime.bindingCalled', (e) => { if (e.name === BINDING) { if (process.env.JARVIS_TRADES_DEBUG) process.stderr.write('push\n'); onPush(); } });
  return c.Runtime.evaluate({ expression: LISTEN, returnByValue: true }).then((r) => r.result?.value === true).catch(() => false);
}

async function snapshot(c) {
  const r = await c.Runtime.evaluate({ expression: SNAPSHOT, returnByValue: true, awaitPromise: true, timeout: 10000 });
  if (r.exceptionDetails) return { status: 'error', error: String(r.exceptionDetails.exception?.description || r.exceptionDetails.text).slice(0, 300) };
  return r.result.value;
}

async function once() {
  let c;
  try { c = await connect(); out(await snapshot(c)); }
  catch (e) { out({ status: 'no_tv', error: e.message }); }
  finally { try { await c?.close(); } catch {} }
}

async function watch() {
  let last = '', lastAt = 0;
  for (;;) {
    let c;
    try {
      c = await connect();
      let closed = false, wake = null, pushed = false, hooked = false;
      c.on('disconnect', () => { closed = true; wake?.(); });
      await c.Runtime.enable();
      hooked = await listen(c, () => { pushed = true; wake?.(); });
      while (!closed) {
        if (!hooked) hooked = (await c.Runtime.evaluate({ expression: LISTEN, returnByValue: true })).result?.value === true;
        else await c.Runtime.evaluate({ expression: LISTEN, returnByValue: true });  // re-hook if TradingView swapped the broker
        pushed = false;
        const s = await snapshot(c);
        const key = JSON.stringify(s);
        if (key !== last || Date.now() - lastAt > HEARTBEAT_MS) { out(s); last = key; lastAt = Date.now(); }
        const wait = s.positions && s.positions.length ? IN_TRADE_MS : IDLE_MS;
        if (!pushed) await new Promise((r) => { wake = r; setTimeout(r, wait); });
        wake = null;
      }
    } catch (e) {
      const s = { status: 'no_tv', error: e.code === 'no_chart' ? 'no chart open' : e.message };
      const key = JSON.stringify(s);
      if (key !== last || Date.now() - lastAt > HEARTBEAT_MS) { out(s); last = key; lastAt = Date.now(); }
    } finally { try { await c?.close(); } catch {} }
    await new Promise((r) => setTimeout(r, RETRY_MS));
  }
}

// Run by the Core: when the Core goes away (even killed), its end of the pipes closes; stop then.
process.stdout.on('error', () => process.exit(0));
if (process.argv[2] !== 'once' && !process.stdin.isTTY) { process.stdin.on('end', () => process.exit(0)); process.stdin.on('error', () => process.exit(0)); process.stdin.resume(); }

if (process.argv[2] === 'once') await once(); else await watch();
