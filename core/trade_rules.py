"""Deval's trade-management rules, run by code (no AI in the loop) once he arms them.

The rules, for a LONG position of 1-3 contracts that he opened himself:

  2 contracts   PT1 +10%: sell 1 (a resting limit order placed when armed).
                Then the runner's stop goes to entry + 0.03 and, once the price has reached
                +25%, trails: 15% below the high (never below +10%), 10% below past +50%.
  3 contracts   PT1 +10%: sell 1 -> stop to entry + 0.03 on 2, PT2 +25% placed.
                PT2 +25%: sell 1 -> runner's stop to +10%, then the same trail.
  1 contract    only the time exit.
  Always        anything still open 30 min before the close (15:30 ET) is closed at market.

Jarvis only ever REDUCES a position he opened: it never buys, adds or reverses, and a stop it
manages only moves up. If Deval changes anything himself (sells, adds, moves the stop, cancels
a profit order), it steps back: cancels its own profit orders and leaves the rest to him.

Orders go through TradingView's broker connection (tv/trades.mjs). Until the rules have been
proven on paper, only the brokers in the config's "brokers" list may be used (default: paper).
Settings can be overridden in %LOCALAPPDATA%\\Jarvis\\trade_rules.json.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.scheduler import ET, market_close

DEFAULTS = {
    "pt1": 0.10,            # first profit: +10%
    "pt2": 0.25,            # second profit (3 contracts): +25%
    "be_offset": 0.03,      # break-even stop = entry + this (covers slippage)
    "lock": 0.10,           # runner's floor once trailing / after PT2: +10%
    "trail_start": 0.25,    # the trail starts once the high reaches +25%
    "trail_wide": 0.15,     # 15% below the high ...
    "tight_after": 0.50,    # ... until the high passes +50%,
    "trail_tight": 0.10,    # then 10% below the high
    "min_step": 0.05,       # don't send a trail move smaller than this
    "tick": 0.01,           # price increment (QQQ options: 1 cent)
    "exit_before_close_min": 30,
    "exit_at": None,        # "HH:MM" ET to override the time exit (testing)
    "time_exit": True,
    "brokers": ["paper"],   # broker ids (substring, any case) the rules may send orders through
    "settle_s": 3.0,        # after an order action, give TradingView this long to show it
    "timeout_s": 8.0,       # an order action with no answer for this long has failed
}

SIZES = (1, 2, 3)
EPS = 1e-9


def config_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis" / "trade_rules.json"


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULTS)
    p = path or config_path()
    try:
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception:  # a broken override file must not stop the Core; use the defaults
        pass
    return cfg


def up(x: float, tick: float) -> float:
    """Round up to the tick (the safe side for a long's stop and for a sell limit)."""
    return round(math.ceil(round(x / tick, 6)) * tick, 6)


def p2(x: float | None) -> str:
    return "–" if x is None else f"{x:.2f}"


@dataclass
class Plan:
    symbol: str
    name: str
    n0: int                 # contracts when armed
    avg: float              # entry price
    broker: str
    armed_at: float
    k: int = 0              # profit targets filled so far
    high: float = 0.0       # highest price since arming
    pt_ids: dict = field(default_factory=dict)   # "1"/"2" -> order id Jarvis placed
    stop_id: str | None = None                   # the stop Jarvis manages (after PT1)
    stop_price: float | None = None              # where Jarvis last put it
    closing: bool = False   # time exit sent

    @property
    def qty_now(self) -> int:
        return self.n0 - self.k

    def pt_price(self, n: int, cfg: dict) -> float:
        return up(self.avg * (1 + cfg["pt1" if n == 1 else "pt2"]), cfg["tick"])

    def runner(self) -> bool:
        return (self.n0 == 2 and self.k == 1) or (self.n0 == 3 and self.k == 2)


class RuleBook:
    """Decides, from each snapshot, which orders should exist; emits actions and events.

    actions: dicts for tv/trades.mjs ({cmd, req, symbol, ...}).
    events:  {"title", "text", "say", "level"} to show and speak.
    """

    def __init__(self, cfg: dict | None = None, store=None):
        self.cfg = cfg or load_config()
        self.store = store          # (load() -> dict|None, save(dict|None)) for surviving a restart
        self.plan: Plan | None = None
        self.offer: dict | None = None
        self.declined: set[str] = set()
        self.inflight: dict[str, dict] = {}
        self.settle_until = 0.0
        self.mismatch_since: float | None = None
        self.last_snap: dict | None = None
        self.last_warn = 0.0
        self._req = itertools.count(1)
        self.restored: dict | None = store.load() if store else None

    # ---------- public state ----------

    def allowed(self, broker_id: str | None) -> bool:
        b = (broker_id or "").lower()
        return bool(b) and any(a.lower() in b for a in self.cfg["brokers"])

    def public(self) -> dict:
        if self.plan:
            p, c = self.plan, self.cfg
            steps = []
            if p.n0 >= 2:
                steps.append({"label": f"PT1 {p2(p.pt_price(1, c))}", "done": p.k >= 1})
            if p.n0 == 3:
                steps.append({"label": f"PT2 {p2(p.pt_price(2, c))}", "done": p.k >= 2})
            return {"state": "armed", "symbol": p.symbol, "n0": p.n0, "k": p.k, "steps": steps,
                    "stop": p.stop_price, "high": p.high or None, "trailing": self._trailing(p)}
        if self.offer:
            return {"state": "offered", **self.offer}
        return {"state": "off"}

    # ---------- arming ----------

    def arm(self, now: float | None = None) -> tuple[list[dict], list[dict], str | None]:
        now = now or time.time()
        snap = self.last_snap
        if self.plan:
            return [], [], "The rules are already armed."
        if not snap or snap.get("status") != "ok":
            return [], [], "I can't see your account right now."
        if not self.allowed(snap.get("broker_id")):
            return [], [], f"The rules only run on {', '.join(self.cfg['brokers'])} for now, not {snap.get('broker')}."
        pos = self._single_long(snap)
        if not pos:
            return [], [], "There's no single long position of 1 to 3 contracts to manage."
        if self.cfg["time_exit"] and self._past_exit(now):
            return [], [], f"It's past the {self._exit_time_text(now)} exit, so I won't arm the rules now."
        self.plan = Plan(symbol=pos["symbol"], name=pos.get("name") or pos["symbol"], n0=int(pos["qty"]),
                         avg=float(pos["avg"]), broker=snap.get("broker_id") or "", armed_at=now,
                         high=float(pos.get("last") or pos["avg"]))
        self.offer = None
        p, c = self.plan, self.cfg
        if p.n0 == 1:
            text = "Trade rules armed: one contract, so only the time exit applies."
        elif p.n0 == 2:
            text = (f"Trade rules armed: PT1 sells 1 at {p2(p.pt_price(1, c))}, then the runner's stop goes to "
                    f"{p2(up(p.avg + c['be_offset'], c['tick']))} and trails from {p2(up(p.avg * (1 + c['trail_start']), c['tick']))}.")
        else:
            text = (f"Trade rules armed: PT1 sells 1 at {p2(p.pt_price(1, c))}, PT2 sells 1 at {p2(p.pt_price(2, c))}, "
                    f"then the runner trails.")
        self._save()
        actions, events = self.step(snap, now)
        return actions, [self._ev("Trade rules armed", text)] + events, None

    def disarm(self, why: str = "Trade rules off.") -> tuple[list[dict], list[dict]]:
        if not self.plan:
            self.offer = None
            return [], []
        actions = self._cancel_my_pts(self.last_snap)
        self.plan = None
        self._save()
        return actions, [self._ev("Trade rules off", why + (" I've cancelled my profit orders." if actions else ""))]

    def decline(self) -> None:
        if self.offer:
            self.declined.add(self._key(self.offer))
        self.offer = None

    # ---------- each reading ----------

    def step(self, snap: dict, now: float | None = None) -> tuple[list[dict], list[dict]]:
        now = now or time.time()
        if snap.get("status") != "ok":
            return [], []
        self.last_snap = snap
        late = self._expire(now)
        acts, events = self._step(snap, now)
        return acts, late + events

    def _step(self, snap: dict, now: float) -> tuple[list[dict], list[dict]]:
        if self.restored is not None:
            self._restore(snap)
        if not self.plan:
            self._update_offer(snap)
            return [], []
        p, c = self.plan, self.cfg
        pos = next((x for x in snap.get("positions") or [] if x["symbol"] == p.symbol), None)
        mine = [o for o in snap.get("working") or [] if o["symbol"] == p.symbol and o["side"] == "sell"]
        if pos is None:  # closed: tidy up my orders, done
            acts = self._cancel_my_pts(snap) + [self._act("cancel", id=o["id"], symbol=p.symbol)
                                                 for o in mine if o["id"] == p.stop_id or p.closing]
            ev = self._ev("Trade rules done", "Position closed; trade rules done.", speak=False)
            self.plan = None
            self._save()
            return self._send(acts, now), [ev]
        if pos.get("last") is not None:
            p.high = max(p.high, float(pos["last"]))
        if self.inflight or now < self.settle_until:
            return [], []

        # what changed since last time?
        q = int(pos["qty"])
        if pos["side"] != "long" or q > p.qty_now:
            return self._takeover("you added to or changed the position")
        if q < p.qty_now:
            # a profit order of mine must have filled, one step per contract
            # filled = my profit order has left the working list (or shows as filled)
            filled = {f["id"] for f in snap.get("fills") or [] if f.get("id")}
            filled |= {oid for oid in p.pt_ids.values() if oid and not any(o["id"] == oid for o in mine)}
            steps = p.qty_now - q
            nxt = [str(p.k + i + 1) for i in range(steps)]
            if all(p.pt_ids.get(n) in filled for n in nxt):
                events = []
                for n in nxt:
                    p.k += 1
                    events.append(self._pt_event(int(n), snap))
                self.mismatch_since = None
                self._save()
                acts, more = self._orders(snap, pos, mine, now)
                return acts, events + more
            if self._grace(now):
                return [], []
            return self._takeover("you sold part of the position yourself")
        # my profit orders should still be working (or filled, waiting for the position to catch up)
        for n, oid in p.pt_ids.items():
            if int(n) > p.k and oid and not any(o["id"] == oid for o in mine):
                if self._grace(now):
                    return [], []
                return self._takeover(f"PT{n} was cancelled")
        self.mismatch_since = None
        return self._orders(snap, pos, mine, now)

    # ---------- results of order actions ----------

    def result(self, res: dict, now: float | None = None) -> tuple[list[dict], list[dict]]:
        now = now or time.time()
        a = self.inflight.pop(str(res.get("req")), None)
        if a is None:
            return [], []
        self.settle_until = now + self.cfg["settle_s"]
        p = self.plan
        ok = bool(res.get("ok"))
        if not ok:
            err = res.get("error") or "rejected"
            if a["cmd"] == "modify" and p and a.get("fallback"):  # e.g. IBKR won't resize a bracket child: replace it
                return self._send([self._act("cancel", id=a["id"], symbol=a["symbol"], then=dict(a["fallback"]))], now), []
            return [], [self._ev("Order failed", f"I couldn't {a['what']}: {err}.", level="warn")]
        if a["cmd"] == "cancel" and a.get("then") and p:
            return self._send([self._act(**a["then"])], now), []
        if not p:
            return [], []
        if a["cmd"] == "place" and a.get("pt"):
            p.pt_ids[str(a["pt"])] = res.get("id")
            self._save()
            return [], [self._ev("Profit order set", f"PT{a['pt']} set: sell {a['qty']} at {p2(a['price'])}.")]
        if a["cmd"] in ("place", "modify") and a.get("kind", "stop") == "stop":
            first = p.stop_price is None
            p.stop_id = res.get("id") or a.get("id")
            p.stop_price = a["price"]
            self._save()
            what = spoken_qty(a["qty"])
            if a.get("trail"):
                return [], [self._ev("Stop raised", f"Stop up to {p2(a['price'])}.", level="info")]
            return [], [self._ev("Stop moved", f"Stop {'set' if first else 'moved'} to {p2(a['price'])} on {what}.")]
        if a["cmd"] == "close":
            return [], [self._ev("Time exit", f"It's {self._exit_time_text(now)}: closed the position at market.")]
        return [], []

    # ---------- internals ----------

    def _orders(self, snap: dict, pos: dict, mine: list[dict], now: float) -> tuple[list[dict], list[dict]]:
        p, c = self.plan, self.cfg
        q = int(pos["qty"])
        acts: list[dict] = []
        # time exit
        if c["time_exit"] and not p.closing and self._past_exit(now):
            p.closing = True
            self._save()
            return self._send([self._act("close", symbol=p.symbol, qty=q, what="close the position")]
                              + self._cancel_my_pts(snap), now), []
        # the stop, once a profit has been taken
        if p.k >= 1:
            want, base = self._stop_target(p)
            stops = [o for o in mine if o["type"] in ("stop", "stop_limit")]
            if len(stops) > 1:
                return self._takeover("there is more than one stop working")
            if not stops:
                acts.append(self._act("place", symbol=p.symbol, kind="stop", qty=q, price=want,
                                      what=f"place a stop at {p2(want)}"))
            else:
                s = stops[0]
                cur = s.get("stop") or 0.0
                if p.stop_id == s["id"] and p.stop_price is not None and abs(cur - p.stop_price) > c["tick"] / 2:
                    return self._takeover("you moved the stop")
                mandatory = int(s.get("qty") or 0) != q or cur < base - EPS
                trail = (want - cur) >= c["min_step"] - EPS
                if mandatory or trail:
                    acts.append(self._act("modify", id=s["id"], symbol=p.symbol, qty=q, price=want, trail=not mandatory,
                                          what=f"move the stop to {p2(want)}",
                                          fallback={"cmd": "place", "symbol": p.symbol, "kind": "stop", "qty": q,
                                                    "price": want, "what": f"place a stop at {p2(want)}"}))
        # then the profit order for the next step (the stop always goes first)
        n = p.k + 1
        if (p.n0 == 2 and n == 1) or (p.n0 == 3 and n in (1, 2)):
            if not p.pt_ids.get(str(n)):
                price = p.pt_price(n, c)
                acts.append(self._act("place", symbol=p.symbol, kind="limit", qty=1, price=price, pt=n,
                                      what=f"place PT{n} at {p2(price)}"))
        return self._send(acts, now), []

    def _stop_target(self, p: Plan) -> tuple[float, float]:
        c = self.cfg
        base = p.avg * (1 + c["lock"]) if (p.n0 == 3 and p.k >= 2) else p.avg + c["be_offset"]
        want = base
        if p.runner() and p.high >= p.avg * (1 + c["trail_start"]) - EPS:
            gap = c["trail_tight"] if p.high >= p.avg * (1 + c["tight_after"]) - EPS else c["trail_wide"]
            want = max(base, p.avg * (1 + c["lock"]), p.high * (1 - gap))
        base_t = up(base, c["tick"])
        return max(up(want, c["tick"]), base_t), base_t

    def _trailing(self, p: Plan) -> bool:
        return p.runner() and p.high >= p.avg * (1 + self.cfg["trail_start"]) - EPS

    def _single_long(self, snap: dict) -> dict | None:
        ps = snap.get("positions") or []
        if len(ps) != 1:
            return None
        pos = ps[0]
        if pos["side"] != "long" or pos.get("avg") is None or int(pos["qty"]) != pos["qty"] or int(pos["qty"]) not in SIZES:
            return None
        return pos

    def _update_offer(self, snap: dict) -> None:
        pos = self._single_long(snap)
        if not pos:
            self.offer = None
            return
        offer = {"symbol": pos["symbol"], "qty": int(pos["qty"]), "avg": pos["avg"],
                 "allowed": self.allowed(snap.get("broker_id")), "brokers": self.cfg["brokers"]}
        self.offer = None if self._key(offer) in self.declined else offer

    @staticmethod
    def _key(o: dict) -> str:
        return f"{o['symbol']}|{o['qty']}|{o['avg']}"

    def _restore(self, snap: dict) -> None:
        saved, self.restored = self.restored, None
        try:
            p = Plan(**saved)
        except Exception:
            return
        pos = next((x for x in snap.get("positions") or [] if x["symbol"] == p.symbol), None)
        if pos and pos["side"] == "long" and abs(float(pos["avg"]) - p.avg) <= 0.02 * p.avg and int(pos["qty"]) <= p.qty_now:
            self.plan = p
        else:
            self._save()

    def _takeover(self, why: str) -> tuple[list[dict], list[dict]]:
        acts = self._cancel_my_pts(self.last_snap)
        self.plan = None
        self.mismatch_since = None
        self._save()
        text = f"You've taken over ({why}); I've stepped back." + (" I've cancelled my profit orders." if acts else "")
        return self._send(acts, time.time()), [self._ev("Trade rules off", text, level="warn")]

    def _cancel_my_pts(self, snap: dict | None) -> list[dict]:
        p = self.plan
        if not p or not snap:
            return []
        working = {o["id"] for o in snap.get("working") or []}
        return [self._act("cancel", id=oid, symbol=p.symbol, what=f"cancel PT{n}")
                for n, oid in p.pt_ids.items() if oid and oid in working and int(n) > p.k]

    def _grace(self, now: float) -> bool:
        """TradingView updates positions and orders separately: allow a moment for them to agree."""
        if self.mismatch_since is None:
            self.mismatch_since = now
        return now - self.mismatch_since < self.cfg["settle_s"]

    def _pt_event(self, n: int, snap: dict) -> dict:
        p, c = self.plan, self.cfg
        fill = next((f for f in snap.get("fills") or [] if f.get("id") == p.pt_ids.get(str(n))), None)
        at = p2((fill or {}).get("avg") or p.pt_price(n, c))
        return self._ev(f"PT{n} filled", f"PT{n} filled at {at}.")

    def _past_exit(self, now: float) -> bool:
        t = self._exit_dt(now)
        return t is not None and datetime.fromtimestamp(now, ET) >= t

    def _exit_dt(self, now: float) -> datetime | None:
        d = datetime.fromtimestamp(now, ET)
        if self.cfg.get("exit_at"):
            hh, mm = map(int, self.cfg["exit_at"].split(":"))
            return d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        close = market_close(d.date())
        if not close:
            return None
        hh, mm = map(int, close.split(":"))
        return d.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(minutes=self.cfg["exit_before_close_min"])

    def _exit_time_text(self, now: float | None = None) -> str:
        t = self._exit_dt(now or time.time())
        return t.strftime("%H:%M") if t else "time"

    def _act(self, cmd: str, **kw) -> dict:
        return {"cmd": cmd, **kw}

    def _send(self, acts: list[dict], now: float) -> list[dict]:
        out = []
        for a in acts:
            a = dict(a)
            a["req"] = str(next(self._req))
            a.setdefault("what", a["cmd"])
            a["sent_at"] = now
            self.inflight[a["req"]] = a
            out.append(a)
        return out

    def _expire(self, now: float) -> list[dict]:
        late = []
        for req, a in list(self.inflight.items()):
            if now - a["sent_at"] > self.cfg["timeout_s"]:
                self.inflight.pop(req)
                late.append(self._ev("Order not confirmed", f"No answer when I tried to {a['what']}; I'll check again.", level="warn"))
        return late

    def _ev(self, title: str, text: str, level: str = "info", speak: bool = True) -> dict:
        return {"title": title, "text": text, "say": (text if speak else None), "level": level}

    def _save(self) -> None:
        if self.store:
            self.store.save(asdict(self.plan) if self.plan else None)


def spoken_qty(n: int) -> str:
    return f"{n} contract{'' if n == 1 else 's'}"


class SettingsStore:
    """Keeps the armed plan in the settings table, so a Core restart mid-trade carries on."""

    KEY = "trade_rules_plan"

    def __init__(self, conn):
        self.conn = conn

    def load(self) -> dict | None:
        row = self.conn.execute("select value from settings where key = ?", (self.KEY,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] != "null" else None
        except ValueError:
            return None

    def save(self, plan: dict | None) -> None:
        self.conn.execute("insert into settings (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                          (self.KEY, json.dumps(plan)))
        self.conn.commit()
