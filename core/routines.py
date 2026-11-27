"""Jarvis's routines (see core/scheduler.py for how they're run)."""

from __future__ import annotations

from core import tradingview
from core.scheduler import Routine


async def premarket_zones() -> dict:
    r = await tradingview.mark_zones()
    nearest = lambda zs: f"{zs[0]['name']} {zs[0]['lo']:.2f}–{zs[0]['hi']:.2f}" if zs else "none"
    return {"summary": f"QQQ {r['price']}: {nearest(r['resistance'])} above, {nearest(r['support'])} below "
                       f"({r['drawn']} zones on {r.get('layout') or 'the chart'})", **r}


def default_routines() -> list[Routine]:
    return [
        Routine(
            name="premarket_zones", title="QQQ zones in TradingView", at="09:27", days="market",
            catch_up_minutes=60, retries=2, retry_minutes=3, run=premarket_zones,
            description="Fresh QQQ 15-min support/resistance zones on the QQQ chart in layout Main, after the "
                        "premarket and before the open (09:27 ET: just after the Tempo desk's 09:25 run).",
        ),
    ]
