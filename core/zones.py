"""Support/resistance zones from 15-minute bars: a faithful port of findZones in
the Tempo desk's src/zones.js (checked against it in tests/test_zones.py).

Zones are fresh and recent: swing highs/lows from the last `lookback_days`
sessions, recent ones weighted more (half-life `half_life_days`), plus the prior
session's high/low/close and today's premarket high/low. Each zone is narrow (at
most `max_width_atr` x ATR and `max_width_pct` of price), 3-4 above (R1..) and
3-4 below (S1..) the price, nearest first.

Pure functions: no network, no clock. Bars are [time (unix s), open, high, low,
close, volume], oldest first.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULTS = {
    "lookback_days": 5, "half_life_days": 1.5, "pivot_bars": 3, "reaction_bars": 8, "max_width_atr": 0.6,
    "max_width_pct": 0.0015, "min_width_ratio": 0.4, "per_side": 4, "min_per_side": 3, "min_gap_atr": 0.75,
    "distance_atr": 12, "max_distance_atr": 10, "key_weight": 2.5,
}
KEY_NAMES = {"pdh": "PDH", "pdl": "PDL", "pdc": "PDC", "pmh": "PMH", "pml": "PML"}


def r2(x: float) -> float:
    """Math.round(x * 100) / 100 (half up, like JavaScript; Python's round is half-even)."""
    return math.floor(x * 100 + 0.5) / 100


def et_date(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def et_hm(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).astimezone(ET).strftime("%H:%M")


def atr(bars: list[dict], n: int = 14) -> float:
    tr = []
    for i in range(max(1, len(bars) - n), len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(tr) / len(tr)


def _first_max(items, key):
    best = items[0]
    for x in items[1:]:
        if key(x) > key(best):
            best = x
    return best


def find_zones(raw: list, premarket: dict | None = None, **opt) -> dict:
    """Zones from bars. `premarket` = {"high", "low", "t"} when the bars don't include
    the premarket session (e.g. regular-session-only data)."""
    o = {**DEFAULTS, **{k: v for k, v in opt.items() if k in DEFAULTS}}
    allb = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": (r[5] if len(r) > 5 else 0) or 0,
             "date": et_date(r[0]), "hm": et_hm(r[0])} for r in raw]
    if len(allb) < 30:
        raise ValueError(f"only {len(allb)} bars")
    last = allb[-1]
    price = last["c"]
    dates = list(dict.fromkeys(b["date"] for b in allb))
    keep = set(dates[-o["lookback_days"]:])
    bars = [b for b in allb if b["date"] in keep]
    A = atr(allb, 14)
    max_w = min(o["max_width_atr"] * A, o["max_width_pct"] * price)
    min_w = max_w * o["min_width_ratio"]

    def age_w(t):
        return 0.5 ** ((last["t"] - t) / 86400 / o["half_life_days"])

    levels = []
    k = o["pivot_bars"]
    for i in range(k, len(bars) - k):
        b = bars[i]
        hi = lo = True
        for j in range(i - k, i + k + 1):
            if j == i:
                continue
            if (bars[j]["h"] >= b["h"]) if j < i else (bars[j]["h"] > b["h"]):
                hi = False
            if (bars[j]["l"] <= b["l"]) if j < i else (bars[j]["l"] < b["l"]):
                lo = False
        nxt = bars[i + 1:i + 1 + o["reaction_bars"]]
        if hi:
            react = (b["h"] - min(x["l"] for x in nxt)) / A
            levels.append({"p": b["h"], "t": b["t"], "kind": "pivot", "w": (1 + min(react, 3) / 2) * age_w(b["t"])})
        if lo:
            react = (max(x["h"] for x in nxt) - b["l"]) / A
            levels.append({"p": b["l"], "t": b["t"], "kind": "pivot", "w": (1 + min(react, 3) / 2) * age_w(b["t"])})

    def rth(b):
        return "09:30" <= b["hm"] < "16:00"

    today = last["date"]
    rth_dates = [d for d in dict.fromkeys(b["date"] for b in allb if rth(b)) if d < today]

    def key(kind, b, p):
        levels.append({"p": p, "t": b["t"], "kind": kind, "w": o["key_weight"]})

    if rth_dates:
        prev = rth_dates[-1]
        pd = [b for b in allb if b["date"] == prev and rth(b)]
        h = _first_max(pd, lambda b: b["h"])
        lw = _first_max(pd, lambda b: -b["l"])
        key("pdh", h, h["h"]); key("pdl", lw, lw["l"]); key("pdc", pd[-1], pd[-1]["c"])
    pm = [b for b in allb if b["date"] == today and "04:00" <= b["hm"] < "09:30"]
    if pm:
        h = _first_max(pm, lambda b: b["h"])
        lw = _first_max(pm, lambda b: -b["l"])
        key("pmh", h, h["h"]); key("pml", lw, lw["l"])
    elif premarket and premarket.get("high") and premarket.get("low"):
        t = premarket.get("t") or last["t"]
        key("pmh", {"t": t}, float(premarket["high"])); key("pml", {"t": t}, float(premarket["low"]))

    # cluster: repeatedly take the price window (at most max_w wide) holding the most weight
    pool = sorted(levels, key=lambda x: x["p"])  # stable, like Array.prototype.sort
    zones = []
    while pool:
        best = None
        j = 0
        for i in range(len(pool)):
            if j < i + 1:
                j = i + 1
            while j < len(pool) and pool[j]["p"] - pool[i]["p"] <= max_w:
                j += 1
            m = pool[i:j]
            s = sum(x["w"] for x in m) + 0.5 * (len(m) - 1)
            if best is None or s > best[2]:
                best = (i, j, s)
        i, j, s = best
        m = pool[i:j]
        del pool[i:j]
        lo, hi = m[0]["p"], m[-1]["p"]
        if hi - lo < min_w:
            mid = (lo + hi) / 2
            lo, hi = mid - min_w / 2, mid + min_w / 2
        zones.append({"lo": lo, "hi": hi, "score": s, "touches": sum(1 for x in m if x["kind"] == "pivot"),
                      "keys": list(dict.fromkeys(KEY_NAMES[x["kind"]] for x in m if x["kind"] != "pivot")),
                      "from": min(x["t"] for x in m), "last_touch": max(x["t"] for x in m)})

    def dist(z):
        return z["lo"] - price if z["lo"] > price else price - z["hi"] if z["hi"] < price else 0

    def gap(a, b):
        return max(a["lo"], b["lo"]) - min(a["hi"], b["hi"])

    def pick(side):
        res = side == "resistance"
        cands = [z for z in zones if ((z["lo"] + z["hi"]) / 2 >= price if res else (z["lo"] + z["hi"]) / 2 < price)]
        cands = [{**z, "rank": z["score"] / (1 + dist(z) / (o["distance_atr"] * A))}
                 for z in cands if dist(z) <= o["max_distance_atr"] * A]
        cands.sort(key=lambda z: -z["rank"])
        out = []
        for z in cands:
            if len(out) >= o["per_side"]:
                break
            if all(gap(c, z) >= o["min_gap_atr"] * A for c in out):
                out.append(z)
        # not enough structure on this side (e.g. price at new highs): round numbers fill in
        step = 5 if price >= 200 else 1 if price >= 50 else 0.5
        n = 1
        while len(out) < o["min_per_side"] and n < 50:
            p = (math.ceil(price / step) * step + (n - 1) * step) if res else (math.floor(price / step) * step - (n - 1) * step)
            z = {"lo": p - min_w / 2, "hi": p + min_w / 2, "score": 0, "touches": 0, "keys": ["round"],
                 "from": last["t"], "last_touch": None}
            if (z["lo"] > price if res else z["hi"] < price) and all(gap(c, z) >= o["min_gap_atr"] * A for c in out):
                out.append(z)
            n += 1
        out.sort(key=dist)
        result = []
        for idx, z in enumerate(out):
            lo, hi = r2(z["lo"]), r2(z["hi"])
            name = f"{'R' if res else 'S'}{idx + 1}"
            touches = f"{z['touches']} touch{'' if z['touches'] == 1 else 'es'}" if z["touches"] else None
            why = " · ".join(x for x in [touches, *z["keys"]] if x)
            result.append({"name": name, "side": side, "lo": lo, "hi": hi, "width": r2(hi - lo),
                           "touches": z["touches"], "keys": z["keys"], "score": r2(z["score"]), "from": z["from"],
                           "last_touch": z["last_touch"],
                           "label": f"{name} {lo:.2f}–{hi:.2f}" + (f" · {why}" if why else "")})
        return result

    return {"price": r2(price), "atr": r2(A), "max_width": r2(max_w), "sessions": [d for d in dates if d in keep],
            "resistance": pick("resistance"), "support": pick("support")}
