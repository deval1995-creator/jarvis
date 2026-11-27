import asyncio
from datetime import date, datetime, timedelta

import pytest

from core import db, scheduler as sch
from core.scheduler import ET, Routine, Scheduler


class Clock:
    def __init__(self, at):
        self.t = at

    def __call__(self):
        return self.t


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "j.db")
    yield c
    c.close()


def make(conn, clock, fail_times=0, **kw):
    calls = []

    async def run():
        calls.append(clock())
        if len(calls) <= fail_times:
            raise RuntimeError("TradingView didn't answer")
        return {"summary": "ok"}

    r = Routine(name="zones", title="Zones", at="09:27", run=run, **{"days": "market", "catch_up_minutes": 60, **kw})
    events = []

    async def on_event(e):
        events.append(e)

    return Scheduler(conn, [r], on_event=on_event, now=clock), calls, events


def tick(s):
    return asyncio.run(s.tick())


def test_market_calendar_knows_holidays_and_half_days():
    assert not sch.is_market_day(date(2026, 11, 26))  # Thanksgiving
    assert sch.market_close(date(2026, 11, 27)) == "13:00"  # day after: half-day
    assert sch.market_close(date(2026, 11, 30)) == "16:00"
    assert not sch.is_market_day(date(2026, 11, 28))  # Saturday


def test_runs_on_time_once_per_day(conn):
    clock = Clock(at(2026, 11, 30, 9, 26))
    s, calls, events = make(conn, clock)
    assert tick(s) == [] and not calls  # not yet
    clock.t = at(2026, 11, 30, 9, 27)
    tick(s)
    clock.t = at(2026, 11, 30, 9, 45)
    tick(s)  # already done today
    assert len(calls) == 1 and events[-1]["status"] == "done"
    assert s.next_run(s.routines["zones"]).startswith("2026-12-01T09:27")


def test_skips_market_holidays(conn):
    clock = Clock(at(2026, 11, 26, 9, 30))  # Thanksgiving
    s, calls, _ = make(conn, clock)
    tick(s)
    assert not calls and s.next_run(s.routines["zones"]).startswith("2026-11-27T09:27")


def test_catches_up_after_sleep_within_the_window(conn):
    clock = Clock(at(2026, 11, 30, 10, 15))  # PC woke 48 min late
    s, calls, _ = make(conn, clock)
    tick(s)
    assert len(calls) == 1


def test_too_late_is_recorded_as_missed_and_reported(conn):
    clock = Clock(at(2026, 11, 27, 9, 30))
    s, calls, events = make(conn, clock)
    tick(s)  # ran on Friday
    clock.t = at(2026, 11, 30, 11, 0)  # Monday: Jarvis was off until 11:00
    calls.clear(); events.clear()
    tick(s)
    tick(s)
    assert not calls and [e["status"] for e in events] == ["missed"]
    assert s.last_run("zones")["status"] == "missed"


def test_failures_retry_then_stop(conn):
    clock = Clock(at(2026, 11, 30, 9, 27))
    s, calls, events = make(conn, clock, fail_times=5, retries=2, retry_minutes=3)
    tick(s)
    tick(s)  # too soon to retry
    assert len(calls) == 1 and events[-1]["status"] == "failed" and events[-1]["retry_in_minutes"] == 3
    conn.execute("update routine_runs set finished_at = ?", ((datetime.now(ET) - timedelta(minutes=4)).isoformat(),))
    tick(s)
    conn.execute("update routine_runs set finished_at = ?", ((datetime.now(ET) - timedelta(minutes=4)).isoformat(),))
    tick(s)
    conn.execute("update routine_runs set finished_at = ?", ((datetime.now(ET) - timedelta(minutes=4)).isoformat(),))
    tick(s)  # 3 attempts used: no more today
    assert len(calls) == 3 and s.last_run("zones")["attempts"] == 3


def test_a_retry_that_succeeds_is_done(conn):
    clock = Clock(at(2026, 11, 30, 9, 27))
    s, calls, events = make(conn, clock, fail_times=1)
    tick(s)
    conn.execute("update routine_runs set finished_at = ?", ((datetime.now(ET) - timedelta(minutes=4)).isoformat(),))
    tick(s)
    assert len(calls) == 2 and s.last_run("zones")["status"] == "done"


def test_manual_run_doesnt_use_up_the_day(conn):
    clock = Clock(at(2026, 11, 30, 8, 0))
    s, calls, _ = make(conn, clock)
    asyncio.run(s.run_now("zones"))
    clock.t = at(2026, 11, 30, 9, 27)
    tick(s)
    assert len(calls) == 2


def test_disabled_routines_never_run(conn):
    clock = Clock(at(2026, 11, 30, 9, 30))
    s, calls, _ = make(conn, clock, enabled=False)
    tick(s)
    assert not calls and s.next_run(s.routines["zones"]) is None


def test_routines_api(tmp_path):
    from fastapi.testclient import TestClient

    from core.api import create_app
    from tests.fakes import FakeBackend

    async def no_review(*a):
        return {"memories": [], "summary": ""}, None

    ran = []

    async def job():
        ran.append(1)
        return {"summary": "marked"}

    r = Routine(name="zones", title="Zones", at="09:27", run=job)
    app = create_app(conn=db.connect(tmp_path / "j.db"), backend_factory=FakeBackend, token="t",
                     review_runner=no_review, routines=[r])
    h = {"X-Jarvis-Token": "t"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        [st] = c.get("/api/routines", headers=h).json()
        assert st["name"] == "zones" and st["at"] == "09:27"
        assert c.post("/api/routines/zones/run", headers=h).json() == {"started": "zones"}
        assert c.post("/api/routines/nope/run", headers=h).status_code == 404


def test_a_new_routine_reports_no_miss_on_its_first_day(conn):
    clock = Clock(at(2026, 11, 30, 14, 0))  # installed this afternoon
    s, calls, events = make(conn, clock)
    tick(s)
    assert not calls and not events and s.next_run(s.routines["zones"]).startswith("2026-12-01T09:27")
