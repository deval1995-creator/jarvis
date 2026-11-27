// Jarvis's trade watcher: reads your broker positions and orders from TradingView Desktop.
//
// TradingView keeps its broker connection (Interactive Brokers) in the page, whether or not
// the trading panel is open. Every 2 s this reads positions, working orders, today's fills
// and the account's daily P&L, and prints one JSON line to stdout when anything changed
// (and a heartbeat every 20 s). READ-ONLY: it never places, changes or cancels anything,
// and never clicks in TradingView.
//
//   node tv/trades.mjs            watch (the Core runs this and reads the lines)
//   node tv/trades.mjs once       one snapshot, then exit

const BRIDGE = (process.env.JARVIS_TV_MCP || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/tradingview-mcp-main').split('\\').join('/');
const { createRequire } = await import('node:module');
const CDP = createRequire(`${BRIDGE}/package.json`)('chrome-remote-interface');
const HOST = '127.0.0.1', PORT = Number(process.env.JARVIS_TV_PORT || 9222);
const EVERY_MS = 2000, HEARTBEAT_MS = 20000, RETRY_MS = 10000;

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
  const open = (positions || []).filter((p) => p && p.qty).map((p) => {
    let pl = num(p.pl);
    if (pl === null) { try { pl = num(svc.find(p.id)?.pl); } catch {} }
    if (pl === null && typeof b.subscribePL === 'function') { try { b.subscribePL(p.id); } catch {} }  // what the panel does to get P&L
    return { id: String(p.id), symbol: p.symbol, name: p.symbolDisplayName || p.symbol, qty: Math.abs(p.qty),
             side: p.side === -1 ? 'short' : 'long', avg: num(p.avgPrice), pl, last: num(p.last) };
  });
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
      let closed = false;
      c.on('disconnect', () => { closed = true; });
      while (!closed) {
        const s = await snapshot(c);
        const key = JSON.stringify(s);
        if (key !== last || Date.now() - lastAt > HEARTBEAT_MS) { out(s); last = key; lastAt = Date.now(); }
        await new Promise((r) => setTimeout(r, EVERY_MS));
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
