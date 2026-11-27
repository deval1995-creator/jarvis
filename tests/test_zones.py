"""core/zones.py must give exactly the zones the Tempo desk's findZones (JavaScript) gives.

Both run on the same generated bars across many market shapes (prices from 45 to 650,
flat/rising/falling, calm/volatile, with and without premarket bars). Skipped when
Node or the desk isn't on this machine.
"""

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from core import zones

DESK = Path(os.environ.get("TEMPO_DESK", r"C:\Users\deval\OneDrive\Desktop\Tempo_Automation\desk"))
JS = Path(__file__).parent / "fixtures" / "js_find_zones.mjs"


def make_bars(rng, start_price, days, drift, vol, with_premarket):
    bars, p, d, day = [], start_price, 0, 0
    t0 = 1795001400
    while d < days:
        base = t0 + day * 86400
        day += 1
        if ((base // 86400) + 4) % 7 in (5, 6):  # weekend
            continue
        d += 1
        for s in (range(-22, 26) if with_premarket else range(0, 26)):  # 15-min slots; negative = premarket
            o = p
            p = max(1, p * (1 + rng.gauss(drift, vol)))
            h = max(o, p) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, p) * (1 - abs(rng.gauss(0, vol / 2)))
            bars.append([base + s * 900, round(o, 2), round(h, 2), round(lo, 2), round(p, 2), rng.randint(1000, 90000)])
    return bars


def cases(n=40, seed=7):
    rng = random.Random(seed)
    return [{"bars": make_bars(rng, rng.choice([45, 120, 480, 650]), rng.choice([3, 5, 8]),
                               rng.choice([-0.0006, 0, 0.0006, 0.002]), rng.choice([0.0008, 0.002, 0.004]), i % 2 == 0),
             "opt": {"lookback_days": rng.choice([3, 5]), "half_life_days": 1.5, "per_side": 4, "min_per_side": 3,
                     "max_width_atr": 0.6, "max_width_pct": 0.0015, "pivot_bars": 3, "min_gap_atr": 0.75,
                     "max_distance_atr": 10}} for i in range(n)]


@pytest.mark.skipif(not (shutil.which("node") and (DESK / "src" / "zones.js").exists()), reason="needs Node and the Tempo desk")
def test_python_matches_the_desks_javascript():
    cs = cases()
    r = subprocess.run(["node", str(JS)], input=json.dumps(cs), capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "TEMPO_DESK": str(DESK)}, timeout=120)
    assert r.returncode == 0 and r.stdout, r.stderr
    js = json.loads(r.stdout)
    for i, (c, want) in enumerate(zip(cs, js)):
        got = zones.find_zones(c["bars"], **c["opt"])
        assert same(got, want), f"case {i} differs"


def same(a, b) -> bool:
    """Equal as values (JSON writes 1 vs 1.0 differently; floats to 1e-9)."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= 1e-9 * max(1, abs(a), abs(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def test_zone_shapes():
    z = zones.find_zones(cases(1, seed=3)[0]["bars"])
    assert 3 <= len(z["resistance"]) <= 4 and 3 <= len(z["support"]) <= 4
    assert all(x["lo"] > z["price"] or x["hi"] >= z["price"] for x in z["resistance"])
    assert [x["name"] for x in z["resistance"]][:3] == ["R1", "R2", "R3"]
    assert all(x["label"].startswith(f"{x['name']} {x['lo']:.2f}–{x['hi']:.2f}") for x in z["resistance"] + z["support"])


def test_rounding_is_half_up_like_javascript():
    assert zones.r2(1.005) == round(1.005 * 100 + 1e-9) / 100 or zones.r2(0.125) == 0.13
    assert zones.r2(0.125) == 0.13 and zones.r2(-0.125) == -0.12  # Math.round(-12.5) = -12


def test_too_few_bars():
    with pytest.raises(ValueError):
        zones.find_zones([[1, 1, 1, 1, 1, 1]] * 10)
