// Jarvis's trade watcher: reads your broker positions and orders from TradingView Desktop.
//
// TradingView keeps its broker connection (Interactive Brokers) in the page, whether or not
// the trading panel is open. This listens to TradingView's own live feeds: the position
// service (fires on every P&L tick from the broker) and the quote stream for each open
// contract (the same last price TradingView shows). Any change is pushed out within ~150 ms;
// it also reads every 1 s as a backstop (every 300 ms while in a trade). One JSON line goes to
// stdout per change, plus a heartbeat every 20 s. It never clicks in TradingView.
//
// ORDERS: only when Deval has armed his trade rules (core/trade_rules.py) does the Core send
// commands on stdin, one JSON line each. Every command is checked here again, against the live
// account, before it goes to the broker: only the brokers in JARVIS_TRADES_BROKERS (default:
// paper), only SELL orders against a LONG position he holds, never more than he holds, stops
// and limits only (plus closing the position at market for the time exit). Nothing that buys,
// adds or reverses can pass.
//
//   node tv/trades.mjs            watch (the Core runs this and reads the lines)
//   node tv/trades.mjs once       one snapshot, then exit

const BRIDGE = (process.env.JARVIS_TV_MCP || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/tradingview-mcp-main').split('\\').join('/');
const { createRequire } = await import('node:module');
const CDP = createRequire(`${BRIDGE}/package.json`)('chrome-remote-interface');
const HOST = '127.0.0.1', PORT = Number(process.env.JARVIS_TV_PORT || 9222);
const IDLE_MS = 1000, IN_TRADE_MS = 300, HEARTBEAT_MS = 20000, RETRY_MS = 10000;
const BINDING = '__jarvisTradesPush';
const ALLOWED = (process.env.JARVIS_TRADES_BROKERS || 'paper').toLowerCase().split(',').map((x) => x.trim()).filter(Boolean);

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
  return { status: 'ok', broker, broker_id: String(meta.id || ''), account, positions: open,
           working: o.filter((x) => x.status === 6 || x.status === 4),   // working or being placed
           fills: o.filter((x) => x.status === 2).sort((a, z) => (a.time || 0) - (z.time || 0)), ...summary };
})()`;

// Runs inside TradingView: one order command, checked against the live account first.
const EXEC = (cmd, allowed) => `(async (cmd, allowed) => {
  const api = window.TradingViewApi;
  const t = api && api.trading();
  const ab = t && t._activeBroker;
  const b = ab && typeof ab.value === 'function' ? ab.value() : ab;
  if (!b) return { ok: false, error: 'no broker' };
  const id = String((b.metainfo() || {}).id || '').toLowerCase();
  if (!allowed.some((a) => id.includes(a))) return { ok: false, error: 'broker ' + id + ' is not allowed for the rules' };
  if (b.connectionStatus() !== 1) return { ok: false, error: 'broker not connected' };
  const orders = await b.orders();
  const find = (oid) => orders.find((o) => String(o.id) === String(oid));
  if (cmd.cmd === 'cancel') {  // cancelling a sell order only ever reduces risk; allowed with or without a position
    const o = find(cmd.id);
    if (!o) return { ok: true, note: 'already gone' };
    if (o.symbol !== cmd.symbol || o.side !== -1) return { ok: false, error: 'not a sell order on ' + cmd.symbol };
    const r = await b.cancelOrder(o.id);
    return { ok: r !== false };
  }
  const pos = (await b.positions()).find((p) => p.symbol === cmd.symbol && p.qty);
  if (!pos) return { ok: false, error: 'no open position in ' + cmd.symbol };
  if (pos.side !== 1) return { ok: false, error: 'only long positions are managed' };
  const held = Math.abs(pos.qty);
  const q = Number(cmd.qty);
  if (!(q > 0) || q > held || !Number.isInteger(q)) return { ok: false, error: 'quantity ' + cmd.qty + ' vs ' + held + ' held' };
  const px = Number(cmd.price);
  if (cmd.cmd === 'place') {
    if (cmd.kind !== 'limit' && cmd.kind !== 'stop') return { ok: false, error: 'only limit and stop orders' };
    if (!(px > 0)) return { ok: false, error: 'bad price' };
    const before = new Set(orders.map((o) => String(o.id)));
    const pre = { symbol: cmd.symbol, side: -1, qty: q, type: cmd.kind === 'limit' ? 1 : 3, duration: { type: 'DAY' } };
    if (cmd.kind === 'limit') pre.limitPrice = px; else pre.stopPrice = px;
    const r = await b.placeOrder(pre);
    if (r === false) return { ok: false, error: 'rejected' };
    let oid = r && (r.orderId || r.id);
    for (let i = 0; !oid && i < 30; i++) {  // find the new order if the broker didn't say
      await new Promise((res) => setTimeout(res, 100));
      const n = (await b.orders()).find((o) => !before.has(String(o.id)) && o.symbol === cmd.symbol && o.side === -1 && o.type === pre.type && o.qty === q);
      if (n) oid = n.id;
    }
    return { ok: true, id: oid ? String(oid) : null };
  }
  if (cmd.cmd === 'modify') {
    const o = find(cmd.id);
    if (!o || o.status !== 6) return { ok: false, error: 'stop order not working' };
    if (o.symbol !== cmd.symbol || o.side !== -1 || (o.type !== 3 && o.type !== 4)) return { ok: false, error: 'only my sell stops can be modified' };
    if (!(px > 0)) return { ok: false, error: 'bad price' };
    const r = await b.modifyOrder({ ...o, qty: q, stopPrice: px });
    return { ok: r !== false, id: String(o.id) };
  }
  if (cmd.cmd === 'close') {
    const r = await b.closePosition(pos.id, q < held ? q : undefined);
    return { ok: r !== false };
  }
  return { ok: false, error: 'unknown command' };
})(${JSON.stringify(cmd)}, ${JSON.stringify(allowed)})`;

let current = null;          // the live connection to TradingView
let pushNow = () => {};      // ask the watch loop for an immediate reading
let queue = Promise.resolve();

function runCommand(cmd) {
  queue = queue.then(async () => {
    const res = { type: 'result', req: cmd.req, ok: false };
    try {
      if (!current) throw new Error('not connected to TradingView');
      const r = await current.Runtime.evaluate({ expression: EXEC(cmd, ALLOWED), returnByValue: true, awaitPromise: true, timeout: 15000 });
      if (r.exceptionDetails) throw new Error(String(r.exceptionDetails.exception?.description || r.exceptionDetails.text).split('\n')[0]);
      Object.assign(res, r.result.value || {});
    } catch (e) { res.error = String(e.message || e).slice(0, 300); }
    out(res);
    pushNow();
  });
}

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
      current = c;
      let closed = false, wake = null, pushed = false, hooked = false;
      c.on('disconnect', () => { closed = true; wake?.(); });
      await c.Runtime.enable();
      hooked = await listen(c, () => { pushed = true; wake?.(); });
      pushNow = () => { pushed = true; wake?.(); };
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
    } finally { current = null; try { await c?.close(); } catch {} }
    await new Promise((r) => setTimeout(r, RETRY_MS));
  }
}

// Run by the Core: when the Core goes away (even killed), its end of the pipes closes; stop then.
process.stdout.on('error', () => process.exit(0));
if (process.argv[2] !== 'once' && !process.stdin.isTTY) {
  process.stdin.on('end', () => process.exit(0)); process.stdin.on('error', () => process.exit(0));
  const { createInterface } = await import('node:readline');
  createInterface({ input: process.stdin }).on('line', (line) => {  // order commands from the Core
    let cmd; try { cmd = JSON.parse(line); } catch { return; }
    if (cmd && cmd.req && cmd.cmd) runCommand(cmd);
  });
}

if (process.argv[2] === 'once') await once(); else await watch();
