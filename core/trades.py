"""Are you in a trade? Watches your broker positions through TradingView Desktop.

`tv/trades.mjs` reads TradingView's broker connection (Interactive Brokers) every 2 s:
open positions, working orders, today's fills and the daily P&L. This module turns
those readings into the HUD's Position panel and entry / exit announcements
(written and spoken). READ-ONLY: nothing here can place, change or cancel an order.

When it can't see your account (TradingView closed, broker disconnected), the panel
says so; it never shows "flat" for a blind spot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from core.scheduler import ET

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parent.parent
WATCHER = REPO / "tv" / "trades.mjs"
STALE_SECONDS = 45  # no reading for this long = the watcher is stuck

# Contract multipliers (P&L per 1.00 move per contract). Options: 100.
FUTURES_MULT = {"NQ": 20, "MNQ": 2, "ES": 50, "MES": 5, "YM": 5, "MYM": 0.5, "RTY": 50, "M2K": 5, "CL": 1000, "MCL": 100,
                "GC": 100, "MGC": 10}

STATUS_TEXT = {
    "no_tv": "Can't see positions: TradingView is closed",
    "loading": "Can't see positions: TradingView is still loading",
    "no_broker": "Can't see positions: TradingView isn't connected to your broker",
    "connecting": "Connecting to your broker…",
    "disconnected": "Can't see positions: the broker connection dropped",
    "error": "Can't see positions: TradingView gave an error",
    "stale": "Can't see positions: the trade watcher stopped answering",
    "starting": "Starting the trade watcher…",
}


# ---------- contracts ----------

def contract(symbol: str, name: str | None = None) -> dict:
    """What a symbol is, for speaking and for P&L: OPRA:QQQ261127P640.0 -> QQQ 640 put."""
    tick = (symbol or "").split(":")[-1]
    m = re.match(r"^([A-Z.]+)(\d{6})([CP])(\d+(?:\.\d+)?)$", tick)
    if m:
        strike = float(m.group(4))
        return {"kind": "option", "root": m.group(1), "right": "put" if m.group(3) == "P" else "call",
                "strike": f"{strike:g}", "expiry": m.group(2), "mult": 100}
    fut = re.match(r"^(M?[A-Z]{1,3}?)(?:1!|[FGHJKMNQUVXZ]\d{2,4})$", tick)
    if fut and fut.group(1) in FUTURES_MULT:
        return {"kind": "future", "root": fut.group(1), "mult": FUTURES_MULT[fut.group(1)]}
    return {"kind": "stock", "root": tick or (name or "?"), "mult": 1}


def spoken_contract(symbol: str, qty: float, name: str | None = None) -> str:
    c = contract(symbol, name)
    n = int(qty) if float(qty).is_integer() else qty
    if c["kind"] == "option":
        return f"{n} {c['root']} {c['strike']} {c['right']}{'' if n == 1 else 's'}"
    if c["kind"] == "future":
        return f"{n} {c['root']} contract{'' if n == 1 else 's'}"
    return f"{n} {c['root']} share{'' if n == 1 else 's'}"


def money(x: float | None, spoken: bool = False) -> str:
    if x is None:
        return "–"
    v = round(x)
    if spoken:
        return ("plus " if v > 0 else "minus " if v < 0 else "") + f"{abs(v):,} dollars"
    return ("+" if v > 0 else "−" if v < 0 else "") + f"${abs(v):,}"


def price(x: float | None) -> str:
    return "–" if x is None else f"{x:.2f}"


# ---------- reading the snapshot ----------

def brackets(pos: dict, working: list[dict]) -> tuple[float | None, float | None]:
    """The stop and target protecting a position: its working exit orders."""
    exit_side = "sell" if pos["side"] == "long" else "buy"
    stop = target = None
    for o in working:
        if o["symbol"] != pos["symbol"] or o["side"] != exit_side:
            continue
        if o["type"] in ("stop", "stop_limit") and o.get("stop") is not None:
            stop = o["stop"]
        elif o["type"] == "limit" and o.get("limit") is not None:
            target = o["limit"]
    return stop, target


def day_trades(fills: list[dict]) -> int:
    """Round trips today: each time a symbol goes from flat to a position (assumes flat at the open)."""
    pos: dict[str, float] = {}
    trades = 0
    for f in sorted(fills, key=lambda f: (f["time"], f["parent"] is not None)):
        q = abs(f.get("filled") or f.get("qty") or 0) * (1 if f["side"] == "buy" else -1)
        before = pos.get(f["symbol"], 0)
        if before == 0 and q:
            trades += 1
        pos[f["symbol"]] = before + q
    return trades


def entry_time(pos: dict, fills: list[dict]) -> float | None:
    side = "buy" if pos["side"] == "long" else "sell"
    times = [f["time"] for f in fills if f["symbol"] == pos["symbol"] and f["side"] == side]
    return max(times) / 1000 if times else None


def view(pos: dict, working: list[dict], fills: list[dict], seen_at: float) -> dict:
    c = contract(pos["symbol"], pos.get("name"))
    stop, target = brackets(pos, working)
    sign = 1 if pos["side"] == "long" else -1
    per_point = pos["qty"] * c["mult"] * sign
    now = pos.get("last")
    if now is None and pos.get("pl") is not None and pos.get("avg") is not None and per_point:
        now = round(pos["avg"] + pos["pl"] / per_point, 4)
    pl = pos.get("pl")
    if pl is None and now is not None and pos.get("avg") is not None:
        pl = (now - pos["avg"]) * per_point
    cost = (pos.get("avg") or 0) * pos["qty"] * c["mult"]
    return {
        "symbol": pos["symbol"], "name": pos.get("name") or pos["symbol"], "side": pos["side"], "qty": pos["qty"],
        "avg": pos.get("avg"), "now": now, "pl": pl, "pl_pct": (pl / cost) if pl is not None and cost else None,
        "stop": stop, "target": target,
        "at_stop": round((stop - pos["avg"]) * per_point, 2) if stop is not None and pos.get("avg") is not None else None,
        "at_target": round((target - pos["avg"]) * per_point, 2) if target is not None and pos.get("avg") is not None else None,
        "since": entry_time(pos, fills) or seen_at,
    }


# ---------- entries and exits ----------

class Tracker:
    """Keeps the last reading and works out what changed."""

    def __init__(self):
        self.state: dict = {"status": "starting", "text": STATUS_TEXT["starting"], "positions": []}
        self.prev: dict[str, dict] | None = None  # symbol -> position, from the last good reading
        self.first_seen: dict[str, float] = {}
        self.updated = 0.0

    def update(self, snap: dict, now: float | None = None) -> list[dict]:
        now = now or time.time()
        self.updated = now
        status = snap.get("status")
        if status != "ok":
            self.state = {"status": status, "text": STATUS_TEXT.get(status, "Can't see positions"),
                          "broker": snap.get("broker"), "positions": self.state.get("positions", []) if status == "connecting" else [],
                          "error": snap.get("error")}
            return []  # never announce from a blind reading; the next good one is compared with the last good one
        working, fills = snap.get("working") or [], snap.get("fills") or []
        current = {p["symbol"]: p for p in snap.get("positions") or []}
        for s in current:
            self.first_seen.setdefault(s, now)
        events: list[dict] = []
        if self.prev is not None:
            for s, p in current.items():
                old = self.prev.get(s)
                if old is None or old["side"] != p["side"]:
                    if old is not None:
                        events.append(self._exit(old, fills))
                    events.append(self._entry(p, working))
                elif p["qty"] != old["qty"]:
                    events.append(self._resize(old, p))
            for s, old in self.prev.items():
                if s not in current:
                    events.append(self._exit(old, fills))
                    self.first_seen.pop(s, None)
        self.prev = current
        views = [view(p, working, fills, self.first_seen.get(p["symbol"], now)) for p in current.values()]
        self.state = {"status": "ok", "broker": snap.get("broker"), "account": snap.get("account"),
                      "text": "In a trade" if views else "Flat", "positions": views,
                      "working": len(working), "daily_pnl": snap.get("daily_pnl"),
                      "unrealized_pnl": snap.get("unrealized_pnl"), "trades_today": day_trades(fills)}
        return events

    @staticmethod
    def _entry(p: dict, working: list[dict]) -> dict:
        stop, target = brackets(p, working)
        what = spoken_contract(p["symbol"], p["qty"], p.get("name"))
        parts = [f"{p['side']} {what} at {price(p.get('avg'))}"]
        if stop is not None:
            parts.append(f"stop {price(stop)}")
        if target is not None:
            parts.append(f"target {price(target)}")
        text = ", ".join(parts)
        warn = "" if stop is not None else " No stop is working."
        return {"kind": "entry", "title": "Trade opened", "text": text + "." + warn,
                "say": f"You're in, sir: {text}.{' There is no stop on it.' if warn else ''}", "level": "info" if stop else "warn"}

    @staticmethod
    def _resize(old: dict, p: dict) -> dict:
        verb = "Added to" if p["qty"] > old["qty"] else "Trimmed"
        what = spoken_contract(p["symbol"], p["qty"], p.get("name"))
        text = f"{verb} the position: now {p['side']} {what}, average {price(p.get('avg'))}"
        return {"kind": "resize", "title": "Position changed", "text": text + ".", "say": text + ", sir.", "level": "info"}

    @staticmethod
    def _exit(old: dict, fills: list[dict]) -> dict:
        exit_side = "sell" if old["side"] == "long" else "buy"
        mine = [f for f in fills if f["symbol"] == old["symbol"] and f["side"] == exit_side]
        last = mine[-1] if mine else None
        how = {"stop": "Stopped out", "stop_limit": "Stopped out", "limit": "Target hit"}.get((last or {}).get("type"), "Closed")
        what = spoken_contract(old["symbol"], old["qty"], old.get("name"))
        c = contract(old["symbol"], old.get("name"))
        pl = None
        if last and last.get("avg") is not None and old.get("avg") is not None:
            pl = (last["avg"] - old["avg"]) * old["qty"] * c["mult"] * (1 if old["side"] == "long" else -1)
        elif old.get("pl") is not None:
            pl = old["pl"]
        at = f" at {price(last['avg'])}" if last and last.get("avg") is not None else ""
        text = f"{how}: {what}{at}" + (f", {money(pl)}" if pl is not None else "")
        say = f"{how}, sir: {what}{at}" + (f", {money(pl, spoken=True)}" if pl is not None else "") + "."
        return {"kind": "exit", "title": "Trade closed", "text": text + ".", "say": say,
                "level": "info" if (pl or 0) >= 0 else "warn"}


# ---------- the watcher process ----------

class TradeWatcher:
    """Runs tv/trades.mjs for as long as the Core runs; restarts it if it dies."""

    def __init__(self, broadcast: Callable[[dict], Awaitable[None]] | None = None,
                 announce: Callable[..., Awaitable[None]] | None = None):
        self.tracker = Tracker()
        self.broadcast = broadcast
        self.announce = announce
        self.proc: asyncio.subprocess.Process | None = None

    def status(self) -> dict:
        s = dict(self.tracker.state)
        if s.get("status") == "ok" and time.time() - self.tracker.updated > STALE_SECONDS:
            s = {"status": "stale", "text": STATUS_TEXT["stale"], "positions": []}
        s["updated"] = self.tracker.updated or None
        return s

    async def handle(self, snap: dict) -> None:
        before = json.dumps(self._public(), sort_keys=True, default=str)
        events = self.tracker.update(snap)
        for e in events:
            log.info("trade: %s", e["text"])
            if self.announce:
                try:
                    await self.announce(e["title"], e["text"], level=e["level"], speak=e["say"])
                except Exception:
                    log.exception("trade announcement failed")
        if self.broadcast and (events or json.dumps(self._public(), sort_keys=True, default=str) != before):
            await self.broadcast({"type": "position", **self.status()})

    def _public(self) -> dict:
        s = dict(self.tracker.state)
        return s

    async def run(self) -> None:
        backoff = 5
        while True:
            started = time.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    "node", str(WATCHER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(REPO), creationflags=0x08000000 if os.name == "nt" else 0)
                assert self.proc.stdout
                async for line in self.proc.stdout:
                    try:
                        snap = json.loads(line)
                    except ValueError:
                        continue
                    await self.handle(snap)
                await self.proc.wait()
                log.warning("trade watcher exited (%s); restarting", self.proc.returncode)
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                log.exception("trade watcher failed")
            await self.handle({"status": "stale"})
            backoff = 5 if time.time() - started > 60 else min(backoff * 2, 120)
            await asyncio.sleep(backoff)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                pass


def describe(state: dict) -> dict:
    """For Jarvis's tool: the state in plain terms."""
    out = {k: state.get(k) for k in ("status", "text", "broker", "daily_pnl", "trades_today", "working")}
    out["positions"] = [{**p, "since_et": datetime.fromtimestamp(p["since"], ET).strftime("%H:%M:%S") if p.get("since") else None}
                        for p in state.get("positions") or []]
    if state.get("status") != "ok":
        out["note"] = "Jarvis can't see the account through TradingView right now; the IBKR connector can still answer."
    return out
