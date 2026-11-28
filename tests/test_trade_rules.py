"""The trade rules against a simulated broker: every action is applied the way TradingView would."""
from datetime import datetime

from core import trade_rules as tr
from core.scheduler import ET

SYM = "OPRA:QQQ261130P640.0"
T0 = datetime(2026, 11, 30, 10, 0, tzinfo=ET).timestamp()  # a Monday, 10:00 ET


class Broker:
    """A tiny paper broker: one long position, working orders, fills, a price."""

    def __init__(self, qty, avg=4.00, stop=3.20, broker_id="paper"):
        self.qty, self.avg, self.last = qty, avg, avg
        self.bid = avg
        self.broker_id = broker_id
        self.orders = {}  # id -> order
        self.fills = []
        self.n = 0
        if stop is not None:
            self._new("stop", qty, stop, parent="entry")
        self.reject_modify = False

    def _new(self, kind, qty, price, parent=None):
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"id": oid, "symbol": SYM, "side": "sell", "qty": qty, "status": 6, "parent": parent,
                            "type": kind, "stop": price if kind == "stop" else None, "limit": price if kind == "limit" else None}
        return oid

    def snap(self):
        pos = [{"id": "1", "symbol": SYM, "name": "QQQ 640 Put", "qty": self.qty, "side": "long", "avg": self.avg,
                "pl": None, "last": self.last, "bid": self.bid}] if self.qty else []
        return {"status": "ok", "broker": "Paper Trading", "broker_id": self.broker_id, "positions": pos,
                "working": [dict(o) for o in self.orders.values()], "fills": list(self.fills)}

    def apply(self, a):
        """Run an action like tv/trades.mjs would and return its result line."""
        r = {"type": "result", "req": a["req"], "ok": True}
        if a["cmd"] == "place":
            assert a["qty"] <= self.qty, "never sell more than is held"
            r["id"] = self._new(a["kind"], a["qty"], a["price"])
        elif a["cmd"] == "modify":
            assert self.orders[a["id"]]["type"] == "stop"
            if self.reject_modify:
                return {**r, "ok": False, "error": "bracket child can't be resized"}
            self.orders[a["id"]].update(qty=a["qty"], stop=a["price"])
        elif a["cmd"] == "cancel":
            self.orders.pop(a["id"], None)
        elif a["cmd"] == "close":
            self.fill_market(self.qty)
        return r

    def fill_market(self, q):
        self.qty -= q
        self.fills.append({"id": f"m{len(self.fills)}", "symbol": SYM, "side": "sell", "avg": self.last, "type": "market"})

    def move(self, price):
        """Price moves (bid and last together): resting orders fill."""
        self.last = self.bid = price
        for oid, o in list(self.orders.items()):
            hit = (o["type"] == "limit" and price >= o["limit"]) or (o["type"] == "stop" and price <= o["stop"])
            if hit and self.qty:
                q = min(o["qty"], self.qty)
                self.qty -= q
                del self.orders[oid]
                self.fills.append({"id": oid, "symbol": SYM, "side": "sell", "avg": o["limit"] or o["stop"], "type": o["type"]})
        if not self.qty:
            for oid, o in list(self.orders.items()):
                if o.get("parent") == "entry":  # a bracket child goes with the position
                    del self.orders[oid]


def run(book, b, now, acts=None, events=None):
    """Feed results back until nothing more happens. Returns all events."""
    events = list(events or [])
    acts = list(acts or [])
    for _ in range(20):
        while acts:
            a = acts.pop(0)
            more_a, more_e = book.result(b.apply(a), now)
            acts += more_a
            events += more_e
        now += 5  # past the settle time
        a2, e2 = book.step(b.snap(), now)
        events += e2
        if not a2:
            return events, now
        acts = a2
    raise AssertionError("rules never settled")


def texts(events):
    return [e["text"] for e in events]


def armed(qty, **kw):
    b = Broker(qty, **kw)
    book = tr.RuleBook(dict(tr.DEFAULTS))
    book.step(b.snap(), T0)
    assert book.public()["state"] == "offered"
    acts, events, err = book.arm(T0)
    assert err is None
    events, now = run(book, b, T0, acts, events)
    return book, b, events, now


def stops(b):
    return [o for o in b.orders.values() if o["type"] == "stop"]


def test_three_contracts_full_run():
    book, b, ev, now = armed(3)
    assert "PT1 waiting at 4.40." in texts(ev)
    b.move(4.40)                                    # PT1 fills
    ev, now = run(book, b, now)
    assert b.qty == 2
    [s] = stops(b)
    assert (s["qty"], s["stop"]) == (2, 4.03)       # break-even + 0.03 on the rest
    assert "PT1 sold, sir. 1 at 4.40." in texts(ev) and "PT2 waiting at 5.00." in texts(ev)
    assert "Stop to break-even: 4.03." in texts(ev)
    b.move(5.00)                                    # PT2 fills
    ev, now = run(book, b, now)
    [s] = stops(b)
    assert b.qty == 1 and (s["qty"], s["stop"]) == (1, 4.40)  # runner locked at +10%
    for px in (5.30, 5.60, 6.20):                   # runs: 15% trail, then 10% past +50%
        b.move(px)
        ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 5.58
    b.move(5.58)                                    # trailing stop hit
    ev, now = run(book, b, now)
    assert b.qty == 0 and not b.orders and book.plan is None


def test_two_contracts_runner_trails_with_a_floor():
    book, b, ev, now = armed(2)
    b.move(4.40)
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.03 and stops(b)[0]["qty"] == 1
    b.move(4.90)                                    # below +25%: no trail yet
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.03
    b.move(5.00)                                    # +25%: 15% below = 4.25, floor +10% = 4.40
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.40
    b.move(5.02)                                    # a move smaller than 0.05 isn't sent
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.40
    b.move(5.60)
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.76
    b.move(5.40)                                    # never moves down
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.76


def test_stop_out_before_pt1_cancels_my_profit_order():
    book, b, ev, now = armed(3)
    b.move(3.20)
    ev, now = run(book, b, now)
    assert b.qty == 0 and not b.orders and book.plan is None


def test_bracket_that_cant_be_resized_is_replaced():
    book, b, ev, now = armed(2)
    b.reject_modify = True
    b.move(4.40)
    ev, now = run(book, b, now)
    [s] = stops(b)
    assert (s["qty"], s["stop"], s["parent"]) == (1, 4.03, None)
    assert "Stop to break-even: 4.03." in texts(ev)


def test_moving_the_stop_yourself_hands_over():
    book, b, ev, now = armed(3)
    b.move(4.40)
    ev, now = run(book, b, now)
    [s] = stops(b)
    s["stop"] = 3.90                                # Deval drags the stop
    ev, now = run(book, b, now)
    assert book.plan is None and "Understood, sir: you moved the stop. I've stepped back. Profit orders cancelled." in texts(ev)
    assert not [o for o in b.orders.values() if o["type"] == "limit"]  # my PT2 cancelled
    assert stops(b)[0]["stop"] == 3.90              # his stop left alone


def test_selling_yourself_hands_over_after_a_moment():
    book, b, ev, now = armed(3)
    b.fill_market(1)                                # Deval sells 1 himself; PT1 still working
    a, e = book.step(b.snap(), now + 10)
    assert book.plan is not None                    # waits for TradingView to catch up
    a, e = book.step(b.snap(), now + 14)
    assert book.plan is None and "sold part" in e[0]["text"]
    assert a and a[0]["cmd"] == "cancel"


def test_time_exit_closes_and_tidies():
    book, b, ev, now = armed(2)
    late = datetime(2026, 11, 30, 15, 30, 1, tzinfo=ET).timestamp()
    acts, ev = book.step(b.snap(), late)
    assert acts[0]["cmd"] == "close"
    ev, now = run(book, b, late, acts, ev)
    assert b.qty == 0 and not b.orders and book.plan is None


def test_half_day_exit_is_30_minutes_before_the_early_close():
    book = tr.RuleBook(dict(tr.DEFAULTS))
    assert book._exit_dt(datetime(2026, 11, 27, 11, 0, tzinfo=ET).timestamp()).strftime("%H:%M") == "12:30"


def test_live_broker_refused_until_allowed():
    b = Broker(2, broker_id="IBKR")
    book = tr.RuleBook(dict(tr.DEFAULTS))
    book.step(b.snap(), T0)
    assert book.public()["allowed"] is False
    acts, events, err = book.arm(T0)
    assert acts == [] and "paper" in err


def test_only_one_to_three_long_contracts_are_offered():
    book = tr.RuleBook(dict(tr.DEFAULTS))
    book.step(Broker(4).snap(), T0)
    assert book.public()["state"] == "off"
    book.step(Broker(1).snap(), T0)
    assert book.public()["state"] == "offered"


def test_one_contract_only_gets_the_time_exit():
    book, b, ev, now = armed(1)
    assert [o for o in b.orders.values() if o["type"] == "limit"] == []
    b.move(4.80)
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 3.20              # his stop, untouched


def test_restart_mid_trade_carries_on():
    class Mem:
        v = None
        def load(self): return self.v
        def save(self, v): self.v = v
    mem = Mem()
    b = Broker(3)
    book = tr.RuleBook(dict(tr.DEFAULTS), store=mem)
    book.step(b.snap(), T0)
    acts, events, _ = book.arm(T0)
    run(book, b, T0, acts, events)
    again = tr.RuleBook(dict(tr.DEFAULTS), store=mem)
    again.step(b.snap(), T0 + 30)
    assert again.plan and again.plan.pt_ids.get("1")


def test_no_arming_after_the_time_exit():
    b = Broker(2)
    book = tr.RuleBook(dict(tr.DEFAULTS))
    late = datetime(2026, 11, 30, 15, 40, tzinfo=ET).timestamp()
    book.step(b.snap(), late)
    acts, events, err = book.arm(late)
    assert acts == [] and "15:30" in err


def test_bracket_stop_becomes_standalone_when_armed():
    book, b, ev, now = armed(3)
    [s] = stops(b)
    assert (s["qty"], s["stop"], s["parent"]) == (3, 3.20, None)   # same stop, no longer tied to the entry
    assert any("standalone" in t for t in texts(ev))
    b.move(4.40)                                                     # PT1: now a plain move, no cancel/replace
    ev, now = run(book, b, now)
    [s2] = stops(b)
    assert s2["id"] == s["id"] and (s2["qty"], s2["stop"]) == (2, 4.03)


def test_stop_moved_by_him_before_pt1_is_followed_not_a_takeover():
    book, b, ev, now = armed(2)
    stops(b)[0]["stop"] = 3.50                                       # he tightens it before PT1
    ev, now = run(book, b, now)
    b.move(4.40)
    ev, now = run(book, b, now)
    assert book.plan is not None and stops(b)[0]["stop"] == 4.03


def test_tonights_takeover_stop_shrinks_then_is_cancelled_when_flat():
    """3 SOL, PT1 sold, then he market-sold 1.9999 himself: his stop for 2 would have opened a short."""
    book, b, ev, now = armed(3)
    b.move(4.40)
    ev, now = run(book, b, now)
    assert b.qty == 2 and stops(b)[0]["qty"] == 2
    b.qty = 0.0001                                   # his market sell of 1.9999
    ev, now = run(book, b, now + 10)
    assert book.plan is None
    [s] = stops(b)
    assert s["qty"] == 0.0001 and s["stop"] == 4.03  # shrunk to what he holds, same price
    assert any("Shrunk it to match" in t for t in texts(ev))
    b.qty = 0                                        # he closes the rest; the stop is left over
    ev, now = run(book, b, now)
    assert not stops(b) and any("Cancelled the leftover stop at 4.03" in t for t in texts(ev))
    assert book.guard is None


def test_stop_shrink_falls_back_to_replace():
    book, b, ev, now = armed(2)
    b.move(4.40)
    ev, now = run(book, b, now)
    b.fill_market(0.5)                               # he sells half a contract's worth (crypto-style)
    ev, now = run(book, b, now + 10)
    b.reject_modify = True
    b.qty = 0.25
    ev, now = run(book, b, now)
    [s] = stops(b)
    assert s["qty"] == 0.25 and any("Replaced it to match" in t for t in texts(ev))


def test_guard_leaves_a_right_sized_stop_alone():
    book, b, ev, now = armed(3)
    b.move(4.40)
    ev, now = run(book, b, now)
    [s] = stops(b)
    s["stop"] = 3.90                                 # he moves his stop: takeover, but the size is right
    ev, now = run(book, b, now)
    assert book.plan is None and stops(b)[0]["qty"] == 2 and stops(b)[0]["stop"] == 3.90


def test_trail_follows_the_bid_not_a_spike_print():
    book, b, ev, now = armed(2)
    b.move(4.40)
    ev, now = run(book, b, now)
    b.move(5.40)                                     # bid 5.40: 15% below = 4.59
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.59
    b.last = 6.20                                    # one print at the ask, bid still 5.40
    ev, now = run(book, b, now)
    assert stops(b)[0]["stop"] == 4.59 and book.plan.high == 5.40
