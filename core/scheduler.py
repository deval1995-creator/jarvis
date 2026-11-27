"""Routines: Jarvis's scheduled jobs, run by the Core (no Claude needed).

Each routine has a time in Eastern Time, the days it runs (NYSE market days,
weekdays or every day) and a catch-up window:

- On time: it runs at `at` (ET).
- Missed-job rule: if the PC was asleep or Jarvis was down at that time, it still
  runs when Jarvis is back, as long as that's within `catch_up_minutes`; after
  that it's recorded as missed and Deval is told.
- Once per day: every run is recorded in routine_runs (one row per routine per
  ET day), so a restart or a catch-up never runs a routine twice.
- Failures retry `retries` times, `retry_minutes` apart.
- Manual runs ("run it now") are recorded separately and don't use up the day's
  scheduled run.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.db import utc_now

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
STALE_RUNNING = timedelta(minutes=15)  # a "running" row this old means Jarvis died mid-run

OnEvent = Callable[[dict], Awaitable[None]]


@functools.lru_cache(maxsize=1)
def _nyse():
    import warnings

    import exchange_calendars as xc

    warnings.filterwarnings("ignore", module="exchange_calendars")
    warnings.filterwarnings("ignore", message=".*Series.values returning an ndarray.*")
    return xc.get_calendar("XNYS")


def is_market_day(d: date) -> bool:
    try:
        return bool(_nyse().is_session(d.isoformat()))
    except Exception:  # outside the calendar's range: fall back to weekdays
        return d.weekday() < 5


def market_close(d: date) -> str | None:
    """'16:00', or '13:00' on half-days; None if closed."""
    if not is_market_day(d):
        return None
    try:
        return _nyse().session_close(d.isoformat()).tz_convert(ET).strftime("%H:%M")
    except Exception:
        return "16:00"


@dataclass
class Routine:
    name: str
    title: str
    at: str                                   # "09:25" ET
    run: Callable[[], Awaitable[dict]]        # returns {"summary": "...", ...}
    days: str = "market"                      # market | weekdays | daily
    catch_up_minutes: int = 30
    retries: int = 2
    retry_minutes: int = 3
    enabled: bool = True
    description: str = ""
    running: bool = field(default=False, init=False)

    def runs_on(self, d: date) -> bool:
        if self.days == "market":
            return is_market_day(d)
        if self.days == "weekdays":
            return d.weekday() < 5
        return True

    def scheduled(self, d: date) -> datetime:
        h, m = map(int, self.at.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, routines: list[Routine], on_event: OnEvent | None = None,
                 now: Callable[[], datetime] | None = None, tick_seconds: float = 20.0,
                 watchers: list[Callable[[datetime], Awaitable[None]]] | None = None):
        self.conn = conn
        self.routines = {r.name: r for r in routines}
        self.watchers = list(watchers or [])  # checked every tick (e.g. heads-ups before releases)
        self.on_event = on_event
        self._now = now or (lambda: datetime.now(ET))
        self.tick_seconds = tick_seconds

    # ---- records ----

    def _row(self, name: str, run_date: str) -> dict | None:
        r = self.conn.execute("select * from routine_runs where routine = ? and run_date = ?", (name, run_date)).fetchone()
        return dict(r) if r else None

    def _has_scheduled_runs(self, name: str) -> bool:
        return self.conn.execute("select 1 from routine_runs where routine = ? and run_date not like '% manual %' limit 1",
                                 (name,)).fetchone() is not None

    def last_run(self, name: str) -> dict | None:
        r = self.conn.execute("select * from routine_runs where routine = ? order by id desc limit 1", (name,)).fetchone()
        return dict(r) if r else None

    async def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:
                log.exception("routine event failed")

    # ---- the decision ----

    def due(self, r: Routine, now: datetime) -> str | None:
        """'run', 'missed', or None."""
        if not r.enabled or r.running:
            return None
        d = now.date()
        if not r.runs_on(d):
            return None
        start = r.scheduled(d)
        if now < start:
            return None
        row = self._row(r.name, d.isoformat())
        if row is None:
            if now <= start + timedelta(minutes=r.catch_up_minutes):
                return "run"
            # a brand-new routine has nothing to have missed yet (manual runs don't count)
            return "missed" if self._has_scheduled_runs(r.name) else None
        if row["status"] == "running" and row["started_at"]:
            if datetime.now(timezone.utc) - datetime.fromisoformat(row["started_at"]) > STALE_RUNNING:
                return "run" if row["attempts"] <= r.retries else None  # Jarvis died mid-run
            return None
        if row["status"] == "failed" and row["attempts"] <= r.retries:
            last = datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            if last is None or datetime.now(timezone.utc) - last >= timedelta(minutes=r.retry_minutes):
                return "run"
        return None

    def next_run(self, r: Routine, now: datetime | None = None) -> str | None:
        now = now or self._now()
        if not r.enabled:
            return None
        d = now.date()
        for i in range(0, 15):
            day = d + timedelta(days=i)
            if not r.runs_on(day):
                continue
            start = r.scheduled(day)
            row = self._row(r.name, day.isoformat())
            if i == 0:
                if row and row["status"] in ("done", "missed", "skipped"):
                    continue
                if row and row["status"] == "failed" and row["attempts"] > r.retries:
                    continue
                if now > start + timedelta(minutes=r.catch_up_minutes) and not row:
                    continue
                return max(start, now).isoformat(timespec="minutes")
            return start.isoformat(timespec="minutes")
        return None

    # ---- running ----

    async def _execute(self, r: Routine, run_date: str, manual: bool = False) -> dict:
        r.running = True
        started = utc_now()
        row = self._row(r.name, run_date)
        attempts = (row["attempts"] if row else 0) + 1
        self.conn.execute(
            "insert into routine_runs (routine, run_date, status, attempts, started_at) values (?, ?, 'running', ?, ?)"
            " on conflict(routine, run_date) do update set status = 'running', attempts = excluded.attempts,"
            " started_at = excluded.started_at, error = null",
            (r.name, run_date, attempts, started))
        await self._emit({"type": "routine", "routine": r.name, "status": "running", "manual": manual})
        t0 = _time.monotonic()
        try:
            result = await r.run()
            summary = str(result.get("summary") or "done")[:300]
            self.conn.execute(
                "update routine_runs set status = 'done', finished_at = ?, summary = ?, result = ? "
                "where routine = ? and run_date = ?",
                (utc_now(), summary, json.dumps(result, ensure_ascii=False, default=str)[:200000], r.name, run_date))
            event = {"type": "routine", "routine": r.name, "title": r.title, "status": "done", "summary": summary,
                     "seconds": round(_time.monotonic() - t0, 1), "manual": manual}
        except Exception as e:
            err = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            log.warning("routine %s failed: %s", r.name, err)
            self.conn.execute(
                "update routine_runs set status = 'failed', finished_at = ?, error = ? where routine = ? and run_date = ?",
                (utc_now(), err, r.name, run_date))
            left = r.retries + 1 - attempts
            event = {"type": "routine", "routine": r.name, "title": r.title, "status": "failed", "error": err,
                     "retry_in_minutes": r.retry_minutes if left > 0 and not manual else None, "manual": manual}
        finally:
            r.running = False
        await self._emit(event)
        return event

    async def tick(self) -> list[dict]:
        now = self._now()
        events = []
        for w in self.watchers:
            try:
                await w(now)
            except Exception:
                log.exception("watcher failed")
        for r in self.routines.values():
            what = self.due(r, now)
            if what == "run":
                events.append(await self._execute(r, now.date().isoformat()))
            elif what == "missed":
                self.conn.execute(
                    "insert or ignore into routine_runs (routine, run_date, status, finished_at, summary)"
                    " values (?, ?, 'missed', ?, ?)",
                    (r.name, now.date().isoformat(), utc_now(),
                     f"missed: Jarvis wasn't running between {r.at} and {r.catch_up_minutes} min after"))
                event = {"type": "routine", "routine": r.name, "title": r.title, "status": "missed"}
                await self._emit(event)
                events.append(event)
        return events

    async def run_now(self, name: str) -> dict:
        r = self.routines.get(name)
        if r is None:
            raise KeyError(name)
        if r.running:
            return {"type": "routine", "routine": name, "status": "running", "note": "already running"}
        stamp = self._now().strftime("%Y-%m-%d manual %H:%M:%S")
        return await self._execute(r, stamp, manual=True)

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    def status(self) -> list[dict]:
        out = []
        now = self._now()
        for r in self.routines.values():
            last = self.last_run(r.name)
            out.append({"name": r.name, "title": r.title, "at": r.at, "days": r.days, "enabled": r.enabled,
                        "description": r.description, "running": r.running, "next_run": self.next_run(r, now),
                        "last": {k: last[k] for k in ("run_date", "status", "summary", "error", "finished_at")} if last else None,
                        "market_close_today": market_close(now.date())})
        return out
