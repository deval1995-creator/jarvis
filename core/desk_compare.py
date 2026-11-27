"""Step 5 (switch-over): compare Jarvis's morning zones with the Tempo desk agent's.

Read-only: the desk writes its last run to %LOCALAPPDATA%/Jarvis/state/zones.json;
Jarvis never changes that file or the desk. A zone matches when a desk zone on the
same side overlaps it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from core import zones as zmath
from core.scheduler import ET


def desk_file() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis" / "state" / "zones.json"


def desk_run(day: str, path: Path | None = None) -> dict | None:
    """The desk's zones run for an ET date, or None if it didn't run that day."""
    try:
        run = json.loads((path or desk_file()).read_text(encoding="utf-8")).get("last_run") or {}
    except (OSError, ValueError):
        return None
    at = run.get("at")
    if not at or datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(ET).date().isoformat() != day:
        return None
    return run


def compare(mine: list[dict], desk: list[dict]) -> dict:
    def overlaps(a, b):
        return a["side"] == b["side"] and a["lo"] <= b["hi"] and b["lo"] <= a["hi"]

    matched = [z["name"] for z in mine if any(overlaps(z, d) for d in desk)]
    only_mine = [f"{z['name']} {z['lo']:.2f}–{z['hi']:.2f}" for z in mine if z["name"] not in matched]
    only_desk = [f"{d['name']} {d['lo']:.2f}–{d['hi']:.2f}" for d in desk if not any(overlaps(d, z) for z in mine)]
    n = max(len(mine), len(desk)) or 1
    return {"matched": len(matched), "of": n, "agreement": round(len(matched) / n, 2),
            "only_jarvis": only_mine, "only_desk": only_desk}


def compare_day(conn: sqlite3.Connection, day: str, path: Path | None = None) -> dict:
    row = conn.execute("select zones, marked_at from zone_days where date = ?", (day,)).fetchone()
    if row is None:
        return {"date": day, "status": "no_jarvis", "summary": "Jarvis didn't mark zones today."}
    run = desk_run(day, path)
    if run is None:
        return {"date": day, "status": "no_desk", "summary": "The desk agent didn't mark zones today (retired or not running)."}
    c = compare(json.loads(row["zones"]), (run.get("resistance") or []) + (run.get("support") or []))
    desk_at = datetime.fromisoformat(run["at"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M")
    status = "match" if c["agreement"] >= 0.75 else "differs"
    summary = (f"{c['matched']}/{c['of']} zones match the desk's ({desk_at} ET run)"
               + (f" · only Jarvis: {', '.join(c['only_jarvis'])}" if c["only_jarvis"] else "")
               + (f" · only desk: {', '.join(c['only_desk'])}" if c["only_desk"] else ""))
    return {"date": day, "status": status, "desk_at": desk_at, "summary": summary, **c}


def streak(conn: sqlite3.Connection) -> int:
    """Consecutive most recent comparisons that matched (from the routine's history)."""
    n = 0
    for (result,) in conn.execute("select result from routine_runs where routine = 'desk_compare' and status = 'done' "
                                  "and run_date not like '% manual %' order by run_date desc"):
        try:
            if json.loads(result or "{}").get("status") != "match":
                break
        except ValueError:
            break
        n += 1
    return n


def save_day(conn: sqlite3.Connection, mark: dict) -> str:
    """Record a morning's zones (from tradingview.mark_zones) for its ET date."""
    zones = mark["resistance"] + mark["support"]
    day = mark.get("date") or zmath.et_date(mark["last_bar_t"])
    conn.execute(
        "insert into zone_days (date, symbol, price, atr, marked_at, zones) values (?, ?, ?, ?, ?, ?) "
        "on conflict(date) do update set symbol = excluded.symbol, price = excluded.price, atr = excluded.atr, "
        "marked_at = excluded.marked_at, zones = excluded.zones",
        (day, mark.get("symbol") or "NASDAQ:QQQ", mark["price"], mark["atr"], str(mark.get("last_bar_t")),
         json.dumps(zones, ensure_ascii=False)))
    return day
