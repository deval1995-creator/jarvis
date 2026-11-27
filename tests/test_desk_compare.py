import json

from core import db, desk_compare as dc

Z = lambda name, side, lo, hi: {"name": name, "side": side, "lo": lo, "hi": hi}
MINE = [Z("R1", "resistance", 645.61, 645.94), Z("R2", "resistance", 649.46, 649.84), Z("S1", "support", 642.71, 643.47)]


def desk_file(tmp_path, at, res, sup):
    f = tmp_path / "zones.json"
    f.write_text(json.dumps({"last_run": {"at": at, "resistance": res, "support": sup}}))
    return f


def test_overlapping_same_side_zones_match():
    desk = [Z("R1", "resistance", 645.67, 646.57), Z("R2", "resistance", 652.0, 653.0), Z("S1", "support", 642.71, 643.47)]
    c = dc.compare(MINE, desk)
    assert c["matched"] == 2 and c["only_jarvis"] == ["R2 649.46–649.84"] and c["only_desk"] == ["R2 652.00–653.00"]


def test_compare_day_reads_the_desk_file(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    conn.execute("insert into zone_days values ('2026-11-30', 'NASDAQ:QQQ', 645, 1.3, '0', ?)", (json.dumps(MINE),))
    f = desk_file(tmp_path, "2026-11-30T14:25:05.000Z", MINE[:2], MINE[2:])
    c = dc.compare_day(conn, "2026-11-30", f)
    assert c["status"] == "match" and c["desk_at"] == "09:25" and c["agreement"] == 1.0
    stale = desk_file(tmp_path, "2026-11-27T15:49:53.104Z", MINE[:2], MINE[2:])
    assert dc.compare_day(conn, "2026-11-30", stale)["status"] == "no_desk"
    assert dc.compare_day(conn, "2026-12-01", f)["status"] == "no_jarvis"


def test_morning_zones_are_recorded_for_the_comparison(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    mark = {"date": "2026-11-30", "last_bar_t": 1796050800, "price": 645.0, "atr": 1.3, "resistance": MINE[:2], "support": MINE[2:]}
    assert dc.save_day(conn, mark) == "2026-11-30"
    dc.save_day(conn, mark)  # a re-run replaces the day, never duplicates it
    assert len(json.loads(conn.execute("select zones from zone_days").fetchone()[0])) == 3
    assert "zone_results" not in {r[0] for r in conn.execute("select name from sqlite_master")}
