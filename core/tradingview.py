"""Jarvis's TradingView adapter: the one way Jarvis drives your TradingView Desktop chart.

It calls tv/bridge.mjs, which uses tradingview-mcp-main's own library over the
desktop app's debug port (127.0.0.1:9222). One lock (in this process and across
processes) so a routine and a request never drive the chart at the same time.

Jarvis only ever removes drawings it made itself, recognised by their text:
zone boxes ("R1 671.20–671.80 …", the same labels the Tempo desk uses) and levels
("J· …"). Everything else on your chart is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import msvcrt
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from core import db
from core import zones as zmath

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "tv" / "bridge.mjs"
ZONE_LABEL_RE = r"^[RS][1-9] \d+\.\d{2}–\d+\.\d{2}"  # same pattern as the Tempo desk's zones
LEVEL_PREFIX = "J· "
LEVEL_RE = r"^J· "
CDP_PORT = 9222
NO_WINDOW, DETACHED, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000008, 0x00000200, 0x01000000

# Jarvis draws only on the chart showing `symbol` in the layout `layout`; it never changes
# a chart's symbol or touches other layouts.
ZONES_CONFIG = {  # the Tempo desk's config/zones.config.json, plus the layout
    "layout": os.environ.get("JARVIS_TV_LAYOUT", "Main"), "symbol": "NASDAQ:QQQ", "timeframe": "15", "bars": 700,
    "lookback_days": 5, "half_life_days": 1.5, "per_side": 4, "min_per_side": 3, "max_width_atr": 0.6,
    "max_width_pct": 0.0015, "pivot_bars": 3, "min_gap_atr": 0.75, "max_distance_atr": 10,
    "colors": {"resistance": "239,83,80", "support": "38,166,154", "fill": 0.22, "border": 0.8},
}


class TradingViewError(RuntimeError):
    pass


def _lock_path() -> Path:
    return db.default_db_path().parent / "tradingview.lock"


_local_lock = asyncio.Lock()


@asynccontextmanager
async def chart_lock(wait: float = 120.0):
    """Only one job drives the chart at a time, in this process and across processes."""
    async with _local_lock:
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        deadline = time.monotonic() + wait
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    f.close()
                    raise TradingViewError("TradingView is busy with another job")
                await asyncio.sleep(0.5)
        try:
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                f.close()


async def bridge(command: str, args: dict | None = None, timeout: float = 120.0) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "node", str(BRIDGE), command, json.dumps(args or {}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(REPO),
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TradingViewError(f"TradingView didn't answer ({command}) within {timeout:.0f} s")
    try:
        result = json.loads(out.decode("utf-8") or "{}")
    except ValueError:
        raise TradingViewError(f"bridge gave no answer: {err.decode('utf-8', 'replace')[:200]}")
    if not result.get("ok"):
        raise TradingViewError(result.get("error") or "TradingView bridge failed")
    return result


def _running() -> bool:
    out = subprocess.run(["tasklist", "/fi", "imagename eq TradingView.exe", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return "TradingView.exe" in out


def find_exe() -> str:
    """TradingView Desktop's exe: the Microsoft Store package's folder changes with each update."""
    if os.environ.get("JARVIS_TRADINGVIEW_EXE") and Path(os.environ["JARVIS_TRADINGVIEW_EXE"]).exists():
        return os.environ["JARVIS_TRADINGVIEW_EXE"]
    loc = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                          "(Get-AppxPackage TradingView.Desktop | Select-Object -First 1).InstallLocation"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip()
    exe = Path(loc) / "TradingView.exe" if loc else None
    if not exe or not exe.exists():
        for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "TradingView" / "TradingView.exe",
                  Path(os.environ.get("PROGRAMFILES", "")) / "TradingView" / "TradingView.exe"):
            if p.exists():
                return str(p)
        raise TradingViewError("TradingView Desktop isn't installed")
    return str(exe)


def _launch() -> bool:
    """Start TradingView with its debug port on 127.0.0.1. If it's open without the port, it's
    reopened (like the Tempo desk does). Returns whether it had to close a running TradingView."""
    reopened = False
    if _running():
        subprocess.run(["taskkill", "/im", "TradingView.exe", "/f"], capture_output=True, creationflags=NO_WINDOW)
        for _ in range(30):
            if not _running():
                break
            time.sleep(0.5)
        reopened = True
    args = [find_exe(), f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1"]
    # Outside Jarvis's process group (breakaway), so restarting Jarvis never closes TradingView.
    flags = DETACHED | NEW_GROUP | NO_WINDOW
    try:
        subprocess.Popen(args, creationflags=flags | BREAKAWAY, close_fds=True)
    except OSError:
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    return reopened


def _where(cfg: dict | None = None) -> dict:
    c = {**ZONES_CONFIG, **(cfg or {})}
    return {"layout": c["layout"], "symbol": c["symbol"]}


async def ensure_running(log_cb=None, cfg: dict | None = None) -> dict:
    """TradingView up with its debug port and the target chart there; launches it if needed (up to ~2 min)."""
    where = _where(cfg)
    try:
        st = await bridge("status", where, timeout=20)
        if st.get("symbol"):
            return st
        raise TradingViewError(f"no chart shows {where['symbol']} in layout \"{st.get('layout')}\" "
                               f"(charts: {', '.join(c['symbol'] for c in st.get('charts', []))}); I won't change a chart's symbol")
    except TradingViewError as e:
        if "isn't open" in str(e) or "no chart shows" in str(e):
            raise  # TradingView is up: wrong layout/chart is for Deval to fix, not a reason to relaunch
        pass
    if log_cb:
        await log_cb("Opening TradingView…")
    reopened = await asyncio.to_thread(_launch)
    if reopened and log_cb:
        await log_cb("TradingView was open without its debug port, so I reopened it.")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            st = await bridge("status", where, timeout=20)
            if st.get("symbol"):
                return st
        except TradingViewError:
            continue
    raise TradingViewError("TradingView didn't start with its debug port")


async def mark_zones(cfg: dict | None = None, log_cb=None) -> dict:
    """Read QQQ 15-min bars, compute the zones, draw them, and put the chart back."""
    cfg = {**ZONES_CONFIG, **(cfg or {})}
    async with chart_lock():
        await ensure_running(log_cb, cfg)
        data = await bridge("read", {**_where(cfg), "timeframe": cfg["timeframe"], "count": cfg["bars"]})
        try:
            z = zmath.find_zones(data["bars"], **cfg)
            last_t = data["bars"][-1][0]
            boxes = [{**x, "to": last_t} for x in z["resistance"] + z["support"]]
            drawn = await bridge("draw_zones", {**_where(cfg), "zones": boxes, "mine_re": ZONE_LABEL_RE,
                                                "colors": cfg["colors"]})
        finally:  # leave the chart the way it was, even if something failed
            before = data.get("before") or {}
            try:
                await bridge("restore", {**_where(cfg), "timeframe": before.get("timeframe")})
            except TradingViewError as e:
                log.warning("couldn't restore the chart's timeframe: %s", e)
    result = {"date": zmath.et_date(last_t), "last_bar_t": last_t, "layout": data.get("layout"), "symbol": data["symbol"], "price": z["price"], "atr": z["atr"], "max_width": z["max_width"],
              "sessions": z["sessions"], "resistance": z["resistance"], "support": z["support"],
              "removed_old": drawn["removed"], "drawn": drawn["drawn"], "bars": len(data["bars"]),
              "chart_left_on": before.get("timeframe"), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (db.default_db_path().parent / "zones_last.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def last_zones() -> dict | None:
    try:
        return json.loads((db.default_db_path().parent / "zones_last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def draw_level(price: float, note: str = "", color: str = "255,181,71") -> dict:
    text = f"{LEVEL_PREFIX}{price:.2f}" + (f" {note.strip()[:40]}" if note.strip() else "")
    async with chart_lock():
        await ensure_running()
        return await bridge("draw_level", {**_where(), "price": float(price), "text": text, "color": color})


async def clear_mine(what: str = "all") -> dict:
    pattern = {"zones": ZONE_LABEL_RE, "levels": LEVEL_RE}.get(what, f"(?:{ZONE_LABEL_RE})|(?:{LEVEL_RE})")
    async with chart_lock():
        await ensure_running()
        return await bridge("clear_mine", {"layout": _where()["layout"], "mine_re": pattern})


def summary(r: dict) -> str:
    line = lambda x: f"{x['name']} {x['lo']:.2f}–{x['hi']:.2f}" + (f" ({', '.join(x['keys'])})" if x["keys"] else "")
    return (f"QQQ {r['price']} · 15-min zones from the last {len(r['sessions'])} sessions (ATR {r['atr']}).\n"
            f"Resistance: {' · '.join(map(line, r['resistance'])) or 'none'}\n"
            f"Support: {' · '.join(map(line, r['support'])) or 'none'}")
