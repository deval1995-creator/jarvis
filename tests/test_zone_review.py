import asyncio
from datetime import date, datetime

import pytest

from core import db, zone_review as zr
from core.scheduler import ET, Routine

ATR = 2.0
R = {"name": "R1", "side": "resistance", "lo": 650.0, "hi": 651.0, "keys": ["PMH"], "touches": 3}
S = {"name": "S1", "side": "support", "lo": 640.0, "hi": 641.0, "keys": [], "touches": 5}


def t(hh, mm, day=30):
    return int(datetime(2026, 11, day, hh, mm, tzinfo=ET).timestamp())


def bar(hh, mm, o, h, l, c, day=30):
    return [t(hh, mm, day), o, h, l, c, 1000]


def test_resistance_held_after_a_one_atr_rejection():
    bars = [bar(9, 30, 648, 649, 647, 648.5), bar(9, 45, 648.5, 650.5, 648, 649.8),  # touch
            bar(10, 0, 649.8, 650.2, 647.9, 648.1)]  # 650 - 647.9 = 2.1 >= 1 ATR
    g = zr.grade(R, bars, ATR)
    assert g["outcome"] == "held" and g["first_touch"] == "09:45" and g["reaction_atr"] == 1.05


def test_resistance_broke_on_a_close_above():
    bars = [bar(9, 30, 648, 650.4, 647.5, 650.1), bar(9, 45, 650.1, 652, 650, 651.6)]
    assert zr.grade(R, bars, ATR)["outcome"] == "broke"


def test_support_held_and_untested_and_unclear():
    held = [bar(9, 30, 643, 643, 640.5, 641.2), bar(9, 45, 641.2, 643.4, 641, 643.2)]  # 643.4 - 641 = 2.4
    assert zr.grade(S, held, ATR)["outcome"] == "held"
    assert zr.grade(S, [bar(9, 30, 645, 646, 644, 645)], ATR)["outcome"] == "untested"
    assert zr.grade(S, [bar(9, 30, 642, 642, 640.6, 641.5), bar(9, 45, 641.5, 641.9, 641.1, 641.4)], ATR)["outcome"] == "unclear"


def test_save_review_and_scorecard(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    mark = {"date": "2026-11-30", "last_bar_t": t(9, 15), "symbol": "NASDAQ:QQQ", "price": 645.0, "atr": ATR,
            "resistance": [R], "support": [S]}
    zr.save_day(conn, mark)
    bars = [bar(9, 0, 645, 645, 645, 645),  # premarket: ignored
            bar(9, 30, 646, 650.5, 646, 648), bar(9, 45, 648, 648.5, 647.5, 647.8),  # R1 held (650-647.5=2.5)
            bar(10, 0, 647.8, 648, 639.5, 639.8)]  # S1 broke (close < 640)
    g = zr.review_day(conn, "2026-11-30", bars)
    assert [(z["name"], z["outcome"]) for z in g["results"]] == [("R1", "held"), ("S1", "broke")]
    assert g["touched"] == 2 and g["hold_rate"] == 0.5
    sc = zr.scorecard(conn, 5)
    assert sc["days"] == 1 and sc["by_rank"]["R1/S1"]["held"] == 1 and set(sc["by_origin"]) == {"premarket", "swing only"}
    w = zr.scorecard_window(sc)
    assert w["panels"][0]["type"] == "stats" and w["panels"][1]["chart"]["kind"] == "bar"


def test_review_without_zones_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="no zones were recorded"):
        zr.review_day(db.connect(tmp_path / "j.db"), "2026-11-30", [])


@pytest.mark.parametrize("day,expected", [
    (date(2026, 11, 27), True),    # Friday (after Thanksgiving): the week's last market day
    (date(2026, 11, 25), False),   # Wednesday: Friday is still to come
    (date(2027, 3, 25), True),     # Thursday before Good Friday
    (date(2027, 3, 26), False),    # Good Friday: market closed
])
def test_week_end_schedule(day, expected):
    async def noop():
        return {}

    assert Routine(name="x", title="x", at="16:30", run=noop, days="week_end").runs_on(day) is expected


def test_empty_scorecard_says_so(tmp_path):
    assert zr.scorecard(db.connect(tmp_path / "j.db"))["days"] == 0
