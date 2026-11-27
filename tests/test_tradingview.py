import asyncio

import pytest

from core import tradingview as tv


def test_summary_and_labels_match_the_desk():
    r = {"price": 649.73, "atr": 2.12, "sessions": ["a"] * 5,
         "resistance": [{"name": "R1", "lo": 649.81, "hi": 650.19, "keys": ["round"]}],
         "support": [{"name": "S1", "lo": 647.76, "hi": 648.55, "keys": []}]}
    assert "R1 649.81–650.19 (round)" in tv.summary(r) and "S1 647.76–648.55" in tv.summary(r)
    import re
    assert re.match(tv.ZONE_LABEL_RE, "R1 671.20–671.80 · 3 touches")
    assert not re.match(tv.ZONE_LABEL_RE, "My own box 671")


def test_only_one_job_drives_the_chart(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "_lock_path", lambda: tmp_path / "tv.lock")
    order = []

    async def job(name):
        async with tv.chart_lock():
            order.append(f"{name}+")
            await asyncio.sleep(0.05)
            order.append(f"{name}-")

    async def both():
        await asyncio.gather(job("a"), job("b"))

    asyncio.run(both())
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])


def test_mark_zones_restores_the_chart_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "_lock_path", lambda: tmp_path / "tv.lock")
    calls = []

    async def fake_bridge(cmd, args=None, timeout=0):
        calls.append(cmd)
        if cmd == "status":
            return {"ok": True, "symbol": "NASDAQ:QQQ"}
        if cmd == "read":
            return {"ok": True, "symbol": "NASDAQ:QQQ", "bars": [[i * 900, 1, 1, 1, 1, 1] for i in range(5)],
                    "before": {"timeframe": "1"}}
        return {"ok": True}

    monkeypatch.setattr(tv, "bridge", fake_bridge)
    with pytest.raises(ValueError):  # too few bars for zones
        asyncio.run(tv.mark_zones())
    assert calls[-1] == "restore"
