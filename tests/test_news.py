import asyncio
from datetime import datetime

import pytest

from core import db, news, routines
from core.scheduler import ET

FEED = [
    {"title": "Core PCE Price Index m/m", "country": "USD", "date": "2026-11-25T08:30:00-05:00", "impact": "High", "forecast": "0.3%", "previous": "0.3%"},
    {"title": "Prelim GDP q/q", "country": "USD", "date": "2026-11-25T08:30:00-05:00", "impact": "High", "forecast": "3.5%", "previous": "3.4%"},
    {"title": "FOMC Statement *bold*", "country": "USD", "date": "2026-11-25T14:00:00-05:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Retail Sales", "country": "USD", "date": "2026-11-25T08:30:00-05:00", "impact": "Medium"},
    {"title": "ECB Speech", "country": "EUR", "date": "2026-11-25T09:00:00-05:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-11-26T08:00:00-05:00", "impact": "Holiday"},
]


def test_red_folder_is_todays_high_impact_usd_only():
    ev = news.red_folder(FEED, "2026-11-25")
    assert [e["title"] for e in ev] == ["Core PCE Price Index m/m", "Prelim GDP q/q", "FOMC Statement bold"]
    assert ev[0]["hm"] == "08:30" and ev[0]["forecast"] == "0.3%"
    assert news.red_folder(FEED, "2026-11-26")[0]["holiday"]


def test_heads_up_groups_releases_at_the_same_time_once():
    ev = news.red_folder(FEED, "2026-11-25")
    now = datetime(2026, 11, 25, 8, 22, tzinfo=ET)
    [group] = news.due_heads_ups(ev, [], now)
    assert [e["title"] for e in group] == ["Core PCE Price Index m/m", "Prelim GDP q/q"]
    assert news.due_heads_ups(ev, [e["id"] for e in group], now) == []  # never twice
    assert news.due_heads_ups(ev, [], datetime(2026, 11, 25, 8, 10, tzinfo=ET)) == []  # too early


def test_feed_is_cached_and_reused_when_it_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "_cache_path", lambda: tmp_path / "news.json")
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("429 Too Many Requests")
        return FEED

    t = datetime(2026, 11, 25, 7, 0, tzinfo=ET).timestamp()
    assert news.week_feed(fetch=fetch, now=t)[0] == FEED
    assert news.week_feed(fetch=fetch, now=t + 60)[0] == FEED and len(calls) == 1  # cached
    assert news.week_feed(fresh=True, fetch=fetch, now=t + 7200)[0] == FEED  # refetch failed: last good copy


def test_times_read_naturally():
    assert news.ampm("08:30") == "8:30 AM" and news.ampm("14:00") == "2:00 PM"
    assert news.spoken_time("08:30") == "8:30 a.m." and news.spoken_time("10:00") == "10 a.m."


class FakeBoard:
    def __init__(self):
        self.shown = []

    async def show(self, w):
        self.shown.append(w)
        return {"id": "w1", **w}


def ctx_with_events(tmp_path, monkeypatch, events):
    said, sent = [], []

    async def fake_today(fresh=False, now=None):
        return {"date": "2026-11-25", "events": events, "fetched_at": "2026-11-25T07:59", "source": "test"}

    monkeypatch.setattr(news, "today", fake_today)

    async def broadcast(e):
        sent.append(e)

    ctx = routines.RoutineContext(board=FakeBoard(), broadcast=broadcast, conn=db.connect(tmp_path / "j.db"))

    async def say(text):
        said.append(text)
        return True

    ctx.say = say
    return ctx, said, sent


def test_morning_brief_shows_and_speaks(tmp_path, monkeypatch):
    ctx, said, sent = ctx_with_events(tmp_path, monkeypatch, news.red_folder(FEED, "2026-11-25"))
    brief = {r.name: r for r in routines.make_routines(ctx)[0]}["premarket_news"]
    r = asyncio.run(brief.run())
    assert "3 release(s)" in r["summary"]
    assert ctx.board.shown[0]["panels"][0]["items"][0]["when"] == "8:30 AM"
    assert said[0].startswith("Good morning, sir. Red-folder news today: Core PCE")
    assert sent[0]["type"] == "announce" and sent[0]["spoken"]


def test_quiet_day_says_nothing(tmp_path, monkeypatch):
    ctx, said, sent = ctx_with_events(tmp_path, monkeypatch, [])
    brief = {r.name: r for r in routines.make_routines(ctx)[0]}["premarket_news"]
    r = asyncio.run(brief.run())
    assert r["summary"] == "No red-folder news today." and said == [] and not ctx.board.shown


def test_heads_up_watcher_speaks_once(tmp_path, monkeypatch):
    ctx, said, sent = ctx_with_events(tmp_path, monkeypatch, news.red_folder(FEED, "2026-11-25"))
    _, [watch] = routines.make_routines(ctx)
    now = datetime(2026, 11, 25, 8, 21, tzinfo=ET)
    asyncio.run(watch(now))
    asyncio.run(watch(now))
    assert said == ["Heads up, sir: Core PCE Price Index m/m and Prelim GDP q/q in 9 minutes."]
    assert sent[0]["level"] == "warn"


@pytest.mark.parametrize("when", [datetime(2026, 11, 26, 8, 21, tzinfo=ET), datetime(2026, 11, 25, 6, 30, tzinfo=ET)])
def test_watcher_quiet_on_holidays_and_early(tmp_path, monkeypatch, when):
    ctx, said, _ = ctx_with_events(tmp_path, monkeypatch, news.red_folder(FEED, "2026-11-25"))
    _, [watch] = routines.make_routines(ctx)
    asyncio.run(watch(when))
    assert said == []
