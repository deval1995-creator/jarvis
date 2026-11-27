"""How did the morning's zones do? Graded after the close, and a scorecard over days.

For each zone, from the bars after it was marked (regular session):
- untested: price never reached it
- held:     after the first touch, price moved at least REACT_ATR x ATR away from the
            zone (back the way it came) before a 15-min bar closed beyond it
- broke:    a 15-min bar closed beyond it first
- unclear:  touched, neither by the close

Resistance is approached from below (beyond = above `hi`); support from above.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from core import zones as zmath

REACT_ATR = 1.0


def grade(zone: dict, bars: list, atr: float, react_atr: float = REACT_ATR) -> dict:
    lo, hi, res = zone["lo"], zone["hi"], zone["side"] == "resistance"
    need = react_atr * atr
    touched_at, best, outcome = None, 0.0, "untested"
    for t, o, h, l, c, *_ in bars:
        touch_bar = False
        if touched_at is None:
            if (h >= lo) if res else (l <= hi):
                touched_at, touch_bar = t, True
            else:
                continue
        # How far price went back the way it came. On the touch bar itself only the close
        # counts: within one 15-min bar we can't tell whether its low came before the touch.
        if touch_bar:
            away = (lo - c) if res else (c - hi)
        else:
            away = (lo - l) if res else (h - hi)
        best = max(best, away)
        if (c > hi) if res else (c < lo):
            outcome = "broke"
            break
        if away >= need:
            outcome = "held"
            break
        outcome = "unclear"
    return {"name": zone["name"], "side": zone["side"], "lo": lo, "hi": hi, "keys": zone.get("keys") or [],
            "touches": zone.get("touches"), "outcome": outcome,
            "first_touch": zmath.et_hm(touched_at) if touched_at else None,
            "reaction_atr": round(best / atr, 2) if touched_at and atr else None}


def save_day(conn: sqlite3.Connection, mark: dict) -> str:
    """Record a morning's zones (from tradingview.mark_zones) for its ET date."""
    zones = mark["resistance"] + mark["support"]
    last_t = max((z.get("to") or 0) for z in zones) if zones and zones[0].get("to") else mark.get("last_bar_t")
    day = mark.get("date") or zmath.et_date(last_t)
    conn.execute(
        "insert into zone_days (date, symbol, price, atr, marked_at, zones) values (?, ?, ?, ?, ?, ?) "
        "on conflict(date) do update set symbol = excluded.symbol, price = excluded.price, atr = excluded.atr, "
        "marked_at = excluded.marked_at, zones = excluded.zones",
        (day, mark.get("symbol") or "NASDAQ:QQQ", mark["price"], mark["atr"], str(last_t),
         json.dumps(zones, ensure_ascii=False)))
    conn.execute("delete from zone_results where date = ?", (day,))
    return day


def review_day(conn: sqlite3.Connection, day: str, bars: list) -> dict:
    """Grade a recorded day's zones against that day's regular-session bars after marking."""
    row = conn.execute("select * from zone_days where date = ?", (day,)).fetchone()
    if row is None:
        raise ValueError(f"no zones were recorded for {day}")
    marked = int(float(row["marked_at"]))
    session = [b for b in bars if zmath.et_date(b[0]) == day and "09:30" <= zmath.et_hm(b[0]) < "16:00" and b[0] > marked]
    if not session:
        raise ValueError(f"no regular-session bars for {day} after the zones were marked")
    graded = [grade(z, session, row["atr"]) for z in json.loads(row["zones"])]
    conn.execute("delete from zone_results where date = ?", (day,))
    for g in graded:
        conn.execute("insert into zone_results (date, name, side, lo, hi, keys, touches, outcome, first_touch, reaction_atr)"
                     " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (day, g["name"], g["side"], g["lo"], g["hi"], json.dumps(g["keys"]), g["touches"], g["outcome"],
                      g["first_touch"], g["reaction_atr"]))
    return {"date": day, **_counts(graded), "results": graded}


def _counts(rows: list[dict]) -> dict:
    n = len(rows)
    touched = [r for r in rows if r["outcome"] != "untested"]
    held = sum(r["outcome"] == "held" for r in rows)
    broke = sum(r["outcome"] == "broke" for r in rows)
    return {"zones": n, "touched": len(touched), "held": held, "broke": broke,
            "unclear": sum(r["outcome"] == "unclear" for r in rows),
            "touch_rate": round(len(touched) / n, 2) if n else None,
            "hold_rate": round(held / (held + broke), 2) if held + broke else None}


def origin(keys: list[str], touches: int | None) -> str:
    k = set(keys or [])
    if "round" in k:
        return "round number"
    if k & {"PDH", "PDL", "PDC"}:
        return "prior day"
    if k & {"PMH", "PML"}:
        return "premarket"
    return "swing only"


def scorecard(conn: sqlite3.Connection, days: int = 5) -> dict:
    dates = [r[0] for r in conn.execute(
        "select distinct date from zone_results order by date desc limit ?", (days,)).fetchall()]
    if not dates:
        return {"days": 0, "note": "No graded days yet: zones are graded after each close."}
    rows = [dict(r) | {"keys": json.loads(r["keys"] or "[]")} for r in conn.execute(
        f"select * from zone_results where date in ({','.join('?' * len(dates))})", dates).fetchall()]
    by_rank, by_origin, by_day = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_rank[f"R{r['name'][1]}/S{r['name'][1]}"].append(r)
        by_origin[origin(r["keys"], r["touches"])].append(r)
        by_day[r["date"]].append(r)
    return {"days": len(dates), "from": min(dates), "to": max(dates), **_counts(rows),
            "by_rank": {k: _counts(v) for k, v in sorted(by_rank.items())},
            "by_origin": {k: _counts(v) for k, v in sorted(by_origin.items())},
            "by_day": {d: _counts(v) for d, v in sorted(by_day.items())}}


def scorecard_window(sc: dict) -> dict:
    """A HUD window for the scorecard."""
    pct = lambda x: "–" if x is None else f"{round(x * 100)}%"
    ranks = list(sc["by_rank"].items())
    origins = list(sc["by_origin"].items())
    return {
        "title": f"Zone scorecard · last {sc['days']} day{'s' if sc['days'] != 1 else ''}",
        "subtitle": f"{sc['from']} to {sc['to']} · held = 1 ATR rejection before a 15-min close through",
        "size": "large", "columns": 2,
        "panels": [
            {"type": "stats", "span": 2, "stats": [
                {"label": "Zones", "value": str(sc["zones"])},
                {"label": "Touched", "value": pct(sc["touch_rate"])},
                {"label": "Held (of decided)", "value": pct(sc["hold_rate"]),
                 "tone": "up" if (sc["hold_rate"] or 0) >= 0.6 else "down" if (sc["hold_rate"] or 0) < 0.4 else "neutral"},
                {"label": "Held / broke", "value": f"{sc['held']} / {sc['broke']}"}]},
            {"type": "chart", "title": "Hold rate by rank", "chart": {
                "kind": "bar", "x": [k for k, _ in ranks], "unit": "%",
                "series": [{"name": "held %", "values": [round((v["hold_rate"] or 0) * 100) for _, v in ranks]}]}},
            {"type": "chart", "title": "Hold rate by what built the zone", "chart": {
                "kind": "bar", "x": [k for k, _ in origins], "unit": "%",
                "series": [{"name": "held %", "values": [round((v["hold_rate"] or 0) * 100) for _, v in origins]}]}},
            {"type": "table", "span": 2, "title": "By day", "table": {
                "columns": ["Date", "Zones", "Touched", "Held", "Broke", "Unclear", "Hold rate"],
                "rows": [[d, str(v["zones"]), str(v["touched"]), str(v["held"]), str(v["broke"]), str(v["unclear"]),
                          pct(v["hold_rate"])] for d, v in sorted(sc["by_day"].items(), reverse=True)]}},
        ],
        "note": "A zone counts as held only after price moved 1 ATR away from it; touched-but-undecided zones are "
                "left out of the hold rate.",
    }
