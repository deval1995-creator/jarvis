"""Jarvis's routines (see core/scheduler.py for how they're run).

Pre-market, every NYSE market day (ET):
  08:00              red-folder news brief: HUD window, spoken, pop-up
  10 min before each release   heads-up: spoken, pop-up (a watcher, checked every tick)
  09:27              QQQ zones on Main's QQQ chart, then the nearest levels and the
                     releases still to come, spoken
Speaking needs the voice to be connected (mic on); otherwise it's the HUD and pop-ups.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from core import db, news, tradingview
from core.scheduler import ET, Routine, is_market_day

log = logging.getLogger(__name__)
VOICE_SAY = "http://127.0.0.1:8766/api/say"
HEADS_UP_MINUTES = 10


@dataclass
class RoutineContext:
    board: object | None = None                              # WindowBoard: windows on the HUD
    broadcast: Callable[[dict], Awaitable[None]] | None = None  # events to the page
    token: str | None = None                                 # to make the voice service speak
    conn: object | None = None

    async def say(self, text: str) -> bool:
        """Speak through the voice service, if a client is connected. Never fails the routine."""
        if not self.token:
            return False

        def post():
            req = urllib.request.Request(VOICE_SAY, data=json.dumps({"text": text}).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Jarvis-Token": self.token})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r).get("spoken", False)

        try:
            return await asyncio.to_thread(post)
        except Exception:
            return False  # voice service down or no one connected

    async def announce(self, title: str, text: str, level: str = "info", speak: str | None = None) -> None:
        spoken = await self.say(speak or text) if speak is not False else False
        if self.broadcast:
            await self.broadcast({"type": "announce", "title": title, "text": text, "level": level, "spoken": spoken,
                                  "say": None if speak is False else (speak or text)})


def _fmt_event(e: dict) -> str:
    extra = ", ".join(x for x in (f"forecast {e['forecast']}" if e["forecast"] else "",
                                  f"previous {e['previous']}" if e["previous"] else "") if x)
    return f"{news.ampm(e['hm'])} {e['title']}" + (f" ({extra})" if extra else "")


def _say_event(e: dict) -> str:
    fc = f", forecast {e['forecast']}" if e["forecast"] else ""
    return f"{e['title']} at {news.spoken_time(e['hm'])}{fc}"


def make_routines(ctx: RoutineContext) -> tuple[list[Routine], list]:
    async def premarket_news() -> dict:
        n = await news.today(fresh=True)
        events = [e for e in n["events"] if not e["holiday"]]
        holidays = [e for e in n["events"] if e["holiday"]]
        if not events:
            msg = "No red-folder news today." + (f" Bank holiday: {holidays[0]['title']}." if holidays else "")
            await ctx.announce("Red-folder news", msg, speak=False)
            return {"summary": msg, **n}
        if ctx.board:
            await ctx.board.show({
                "title": "Red-folder news · today", "subtitle": "high impact, USD · times ET", "size": "small",
                "panels": [{"type": "items", "items": [
                    {"when": news.ampm(e["hm"]), "title": e["title"],
                     "detail": " · ".join(x for x in (f"forecast {e['forecast']}" if e["forecast"] else "",
                                                       f"previous {e['previous']}" if e["previous"] else "") if x)}
                    for e in events]}],
                "note": f"{n['source']} · fetched {n['fetched_at'][11:16]} ET",
            })
        spoken = "Good morning, sir. Red-folder news today: " + "; ".join(_say_event(e) for e in events) + "."
        await ctx.announce("Red-folder news", " · ".join(_fmt_event(e) for e in events), speak=spoken)
        return {"summary": f"{len(events)} release(s): " + " · ".join(_fmt_event(e) for e in events), **n}

    async def premarket_zones() -> dict:
        r = await tradingview.mark_zones()
        near = lambda zs: f"{zs[0]['lo']:.2f} to {zs[0]['hi']:.2f}" if zs else "none"
        nearest = lambda zs: f"{zs[0]['name']} {zs[0]['lo']:.2f}–{zs[0]['hi']:.2f}" if zs else "none"
        later = []
        try:
            later = news.later_today((await news.today())["events"], datetime.now(ET))
        except Exception:
            pass
        text = (f"QQQ {r['price']}: {nearest(r['resistance'])} above, {nearest(r['support'])} below"
                + (f" · red folder later: {', '.join(_fmt_event(e) for e in later)}" if later else ""))
        spoken = (f"Zones are marked, sir. Nearest resistance {near(r['resistance'])}, support {near(r['support'])}."
                  + (" Still to come: " + "; ".join(_say_event(e) for e in later) + "." if later else ""))
        await ctx.announce("QQQ zones", text, speak=spoken)
        return {"summary": text + f" ({r['drawn']} zones on {r.get('layout') or 'the chart'})", **r}

    # ---- heads-up before each release (checked every scheduler tick) ----
    def _alerted_load(day: str) -> list[str]:
        if ctx.conn is None:
            return []
        row = ctx.conn.execute("select value from settings where key = 'news_alerted'").fetchone()
        try:
            d = json.loads(row[0]) if row else {}
        except ValueError:
            d = {}
        return d.get("ids", []) if d.get("date") == day else []

    def _alerted_save(day: str, ids: list[str]) -> None:
        if ctx.conn is not None:
            ctx.conn.execute("insert into settings (key, value) values ('news_alerted', ?) "
                             "on conflict(key) do update set value = excluded.value",
                             (json.dumps({"date": day, "ids": ids}),))

    async def heads_up_watcher(now: datetime) -> None:
        if not is_market_day(now.date()) or not ("07:00" <= now.strftime("%H:%M") <= "17:00"):
            return
        try:
            events = (await news.today(now=now))["events"]
        except Exception:
            return  # the 08:00 brief reports feed trouble
        day = now.date().isoformat()
        alerted = _alerted_load(day)
        for group in news.due_heads_ups(events, alerted, now, HEADS_UP_MINUTES):
            mins = max(1, round((datetime.fromisoformat(group[0]["at"]) - now).total_seconds() / 60))
            text = f"in {mins} min ({news.ampm(group[0]['hm'])} ET): " + " · ".join(_fmt_event(e) for e in group)
            spoken = f"Heads up, sir: {' and '.join(e['title'] for e in group)} in {mins} minutes."
            await ctx.announce("Red-folder news soon", text, level="warn", speak=spoken)
            alerted += [e["id"] for e in group]
            _alerted_save(day, alerted)

    routines = [
        Routine(name="premarket_news", title="Red-folder news brief", at="08:00", days="market",
                catch_up_minutes=55, retries=4, retry_minutes=5, run=premarket_news,
                description="Today's high-impact USD releases (ForexFactory) on the HUD, spoken; a heads-up "
                            f"{HEADS_UP_MINUTES} min before each."),
        Routine(name="premarket_zones", title="QQQ zones in TradingView", at="09:27", days="market",
                catch_up_minutes=60, retries=2, retry_minutes=3, run=premarket_zones,
                description="Fresh QQQ 15-min zones on Main's QQQ chart, then the nearest levels and the releases "
                            "still to come, spoken (09:27 ET: just after the Tempo desk's 09:25 run)."),
    ]
    return routines, [heads_up_watcher]
