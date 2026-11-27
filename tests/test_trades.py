import asyncio

from core import trades as tr

SYM = "OPRA:QQQ261127P640.0"
NAME = "QQQ Nov27'26 640 Put"


def snap(positions=(), working=(), fills=(), daily=-545.2, status="ok"):
    return {"status": status, "broker": "Interactive Brokers", "account": "U1", "positions": list(positions),
            "working": list(working), "fills": list(fills), "daily_pnl": daily}


def pos(qty=5, avg=4.12, pl=215.0, side="long", symbol=SYM):
    return {"id": "1", "symbol": symbol, "name": NAME, "qty": qty, "side": side, "avg": avg, "pl": pl, "last": None}


STOP = {"id": "s", "symbol": SYM, "type": "stop", "side": "sell", "qty": 5, "stop": 3.19, "limit": None, "status": 6}
TARGET = {"id": "t", "symbol": SYM, "type": "limit", "side": "sell", "qty": 5, "limit": 5.91, "stop": None, "status": 6}


def fill(t, side, qty, typ, avg, parent=None, symbol=SYM):
    return {"symbol": symbol, "side": side, "qty": qty, "filled": qty, "type": typ, "avg": avg, "time": t, "parent": parent}


def test_contracts_read_naturally():
    assert tr.spoken_contract(SYM, 5) == "5 QQQ 640 puts"
    assert tr.spoken_contract("OPRA:QQQ261127C650.0", 1) == "1 QQQ 650 call"
    assert tr.contract("CME_MINI:NQ1!")["mult"] == 20 and tr.contract("CME_MINI:MNQ1!")["mult"] == 2
    assert tr.spoken_contract("NASDAQ:QQQ", 100) == "100 QQQ shares"


def test_first_reading_is_a_baseline_not_an_entry():
    t = tr.Tracker()
    assert t.update(snap([pos()], [STOP, TARGET])) == []
    assert t.state["text"] == "In a trade" and t.state["positions"][0]["stop"] == 3.19


def test_entry_is_announced_with_stop_and_target():
    t = tr.Tracker()
    t.update(snap())
    [e] = t.update(snap([pos()], [STOP, TARGET], [fill(1_000, "buy", 5, "market", 4.12)]))
    assert e["kind"] == "entry"
    assert e["say"] == "You're in, sir: long 5 QQQ 640 puts at 4.12, stop 3.19, target 5.91."
    v = t.state["positions"][0]
    assert v["now"] == 4.55 and v["at_stop"] == -465.0 and round(v["at_target"]) == 895 and v["since"] == 1.0


def test_entry_without_a_stop_is_a_warning():
    t = tr.Tracker()
    t.update(snap())
    [e] = t.update(snap([pos()]))
    assert e["level"] == "warn" and "no stop" in e["say"].lower()


def test_stopped_out_with_pnl():
    t = tr.Tracker()
    t.update(snap())
    t.update(snap([pos()], [STOP, TARGET]))
    [e] = t.update(snap([], [], [fill(1_000, "buy", 5, "market", 4.12), fill(2_000, "sell", 5, "stop", 3.19, parent="x")]))
    assert e["kind"] == "exit" and e["text"] == "Stopped out: 5 QQQ 640 puts at 3.19, −$465."
    assert e["say"] == "Stopped out, sir: 5 QQQ 640 puts at 3.19, minus 465 dollars."
    assert t.state["text"] == "Flat"


def test_target_hit_and_resize():
    t = tr.Tracker()
    t.update(snap())
    t.update(snap([pos(qty=2)], [STOP, TARGET]))
    [r] = t.update(snap([pos(qty=5)], [STOP, TARGET]))
    assert r["kind"] == "resize" and "now long 5 QQQ 640 puts" in r["text"]
    [e] = t.update(snap([], [], [fill(3_000, "sell", 5, "limit", 5.91)]))
    assert e["text"].startswith("Target hit") and "+$895" in e["text"]


def test_blind_readings_never_announce_and_never_look_flat():
    t = tr.Tracker()
    t.update(snap())
    t.update(snap([pos()], [STOP]))
    assert t.update({"status": "no_tv", "error": "connect ECONNREFUSED"}) == []
    assert t.state["status"] == "no_tv" and t.state["text"].startswith("Can't see positions")
    # it closed while TradingView was shut: the next good reading reports the exit
    [e] = t.update(snap([], [], [fill(5_000, "sell", 5, "stop", 3.19)]))
    assert e["kind"] == "exit"


def test_todays_real_fills_are_four_trades():
    p643, p640 = "OPRA:QQQ261127P643.0", SYM
    fills = [fill(1, "buy", 1, "market", 4.75, symbol=p643), fill(2, "sell", 1, "market", 5.10, symbol=p643),
             fill(3, "buy", 1, "market", 6.18, symbol=p643), fill(4, "sell", 1, "stop", 4.95, "a", symbol=p643),
             fill(5, "buy", 2, "market", 4.18), fill(5, "sell", 2, "stop", 3.27, "b"),
             fill(6, "buy", 5, "market", 3.68), fill(6, "sell", 5, "stop", 3.16, "c")]
    assert tr.day_trades(fills) == 4


def test_watcher_broadcasts_and_announces(monkeypatch):
    sent, said = [], []

    async def broadcast(e):
        sent.append(e)

    async def announce(title, text, level="info", speak=None):
        said.append(speak)

    w = tr.TradeWatcher(broadcast=broadcast, announce=announce)
    asyncio.run(w.handle(snap()))
    asyncio.run(w.handle(snap([pos()], [STOP, TARGET])))
    assert said == ["You're in, sir: long 5 QQQ 640 puts at 4.12, stop 3.19, target 5.91."]
    assert sent[-1]["type"] == "position" and sent[-1]["positions"][0]["qty"] == 5
    assert tr.describe(w.status())["positions"][0]["since_et"]
