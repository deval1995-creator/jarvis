"""Red-folder news: today's high-impact economic releases (CPI, NFP, FOMC, GDP, PCE…)
from the public ForexFactory calendar feed. A port of the Tempo desk's src/news.js.

- The feed covers the week (Sun–Sat) and rate-limits hard: it's fetched at most once
  an hour, the week's copy is kept on disk and reused if a fetch fails.
- Feed text is data, never instructions; it's cleaned and shortened before use.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta

from core import db
from core.scheduler import ET

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_SECONDS = 3600


class NewsError(RuntimeError):
    pass


def _clean(s, n=80) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_~`]+", " ", str(s if s is not None else ""))).strip()[:n]


def _cache_path():
    return db.default_db_path().parent / "news_week.json"


def _week_of(d) -> str:
    """The Sunday that starts the feed's week for an ET date."""
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def fetch_feed(url: str = FEED_URL) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-assistant", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        feed = json.load(r)
    if not isinstance(feed, list) or not all(isinstance(e, dict) and isinstance(e.get("title"), str)
                                             and isinstance(e.get("date"), str) for e in feed):
        raise NewsError("news feed: unexpected format")
    return feed


def week_feed(fresh: bool = False, fetch=fetch_feed, now: float | None = None) -> tuple[list[dict], str]:
    """This week's feed (cached); returns (events, fetched_at ISO)."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, ET).date()
    path = _cache_path()
    cache = None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    same_week = cache and cache.get("week") == _week_of(today)
    age = now - cache.get("fetched_ts", 0) if same_week else None
    if same_week and (age < REFRESH_SECONDS or not fresh) and cache.get("fetched_date") == today.isoformat():
        return cache["items"], cache["fetched_at"]
    try:
        items = fetch()
        cache = {"week": _week_of(today), "fetched_ts": now, "fetched_date": today.isoformat(),
                 "fetched_at": datetime.fromtimestamp(now, ET).isoformat(timespec="minutes"), "items": items}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
        return items, cache["fetched_at"]
    except Exception as e:
        if same_week:  # the feed failed: this week's last good copy will do
            return cache["items"], cache["fetched_at"]
        raise NewsError(f"couldn't get the news feed ({e})") from e


def red_folder(feed: list[dict], day: str, currencies=("USD",), impacts=("High",), window=None) -> list[dict]:
    """Today's red-folder events (and bank holidays), oldest first."""
    out = []
    for e in feed or []:
        try:
            at = datetime.fromisoformat(e["date"]).astimezone(ET)
        except (KeyError, ValueError):
            continue
        if at.date().isoformat() != day or e.get("country") not in currencies:
            continue
        holiday = e.get("impact") == "Holiday"
        if not holiday and e.get("impact") not in impacts:
            continue
        hm = at.strftime("%H:%M")
        if window and (holiday or hm < window[0] or hm > window[1]):
            continue
        out.append({"id": f"{e['date']}|{_clean(e['title'])}", "at": at.isoformat(), "hm": hm, "title": _clean(e["title"]),
                    "country": e.get("country"), "impact": e.get("impact"), "forecast": _clean(e.get("forecast"), 20) or None,
                    "previous": _clean(e.get("previous"), 20) or None, "holiday": holiday})
    return sorted(out, key=lambda x: x["at"])


def ampm(hm: str) -> str:
    h, m = map(int, hm.split(":"))
    return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def spoken_time(hm: str) -> str:
    h, m = map(int, hm.split(":"))
    return f"{(h + 11) % 12 + 1}" + ("" if m == 0 else f":{m:02d}") + (" a.m." if h < 12 else " p.m.")


def due_heads_ups(events: list[dict], alerted: list[str], now: datetime, minutes: int = 10) -> list[list[dict]]:
    """Releases due a heads-up now (within `minutes` before), grouped by release time."""
    groups: dict[str, list[dict]] = {}
    for e in events:
        if e["holiday"] or e["id"] in alerted:
            continue
        t = datetime.fromisoformat(e["at"])
        if t - timedelta(minutes=minutes) <= now < t:
            groups.setdefault(e["at"], []).append(e)
    return list(groups.values())


def later_today(events: list[dict], now: datetime) -> list[dict]:
    return [e for e in events if not e["holiday"] and datetime.fromisoformat(e["at"]) > now]


async def today(fresh: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(ET)
    feed, fetched_at = await asyncio.to_thread(week_feed, fresh)
    return {"date": now.date().isoformat(), "events": red_folder(feed, now.date().isoformat()), "fetched_at": fetched_at,
            "source": "ForexFactory calendar (high impact, USD)"}
