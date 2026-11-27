"""Windows Jarvis can open on Deval's screen, as an in-process MCP server.

Claude fetches the data (IBKR portfolio, calendar, memory, anything) with its
other tools, then calls `show_window` and decides how to show it: the window's
size, its column layout, and a list of panels, each one of

    stats   big headline numbers
    table   rows and columns
    items   a timeline / schedule
    chart   line, area, bar or pie/donut, one or more series
    graph   a network of linked nodes
    text    plain explanation

The Core keeps the open windows and broadcasts them; the page draws them as
floating, resizable HUD windows. Everything is plain data on the page (never
HTML or code), so nothing a window carries can run.

Tool names as Claude sees them: mcp__display__show_window, mcp__display__close_window.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from core.db import utc_now

SERVER_NAME = "display"
TOOL_NAMES = ["show_window", "close_window"]
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES]

MAX_WINDOWS = 6
MAX_PANELS = 8
MAX_ROWS, MAX_COLS, MAX_ITEMS, MAX_STATS, MAX_TEXT = 200, 12, 100, 8, 600
MAX_NODES, MAX_EDGES = 150, 400
MAX_SERIES, MAX_POINTS = 8, 500
TONES = {"up", "down", "warn", "neutral"}
SIZES = {"small", "medium", "large", "full"}
PANEL_TYPES = ("stats", "table", "items", "chart", "graph", "text", "image")
MAX_IMAGE_CHARS = 3_000_000  # ~2 MB of base64
CHART_KINDS = {"line", "area", "bar", "pie", "donut"}

OnChange = Callable[[dict], Awaitable[None]]

_STATS = {"type": "array", "description": "Headline numbers shown large.",
          "items": {"type": "object", "properties": {
              "label": {"type": "string"}, "value": {"type": "string"},
              "tone": {"type": "string", "enum": sorted(TONES), "description": "up = gain/green, down = loss/red"},
          }, "required": ["label", "value"]}}
_TABLE = {"type": "object", "description": "Rows and columns. Cells are strings; start gains with '+' and losses with '-'.",
          "properties": {"columns": {"type": "array", "items": {"type": "string"}},
                         "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
          "required": ["columns", "rows"]}
_ITEMS = {"type": "array", "description": "A timeline: schedule, history, plan.",
          "items": {"type": "object", "properties": {
              "when": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"},
          }, "required": ["title"]}}
_GRAPH = {"type": "object", "description": "A network of linked things (memories, people, topics, dependencies).",
          "properties": {
              "nodes": {"type": "array", "items": {"type": "object", "properties": {
                  "id": {"type": "string"}, "label": {"type": "string"},
                  "group": {"type": "string", "description": "Colours nodes, e.g. person, topic, memory"},
                  "detail": {"type": "string", "description": "Shown when hovered"},
                  "size": {"type": "number", "description": "1 (small) to 5 (large)"},
              }, "required": ["id", "label"]}},
              "edges": {"type": "array", "items": {"type": "object", "properties": {
                  "from": {"type": "string"}, "to": {"type": "string"}, "label": {"type": "string"},
              }, "required": ["from", "to"]}},
          }, "required": ["nodes"]}
_CHART = {"type": "object", "description": "A chart.",
          "properties": {
              "kind": {"type": "string", "enum": sorted(CHART_KINDS),
                       "description": "line/area: values over time; bar: compare amounts (negatives go red); "
                                      "pie/donut: shares of a whole (first series only)"},
              "x": {"type": "array", "items": {"type": "string"},
                    "description": "Category or time labels, one per value (slice names for pie)"},
              "series": {"type": "array", "items": {"type": "object", "properties": {
                  "name": {"type": "string"},
                  "values": {"type": "array", "items": {"type": ["number", "null"]}},
              }, "required": ["values"]}},
              "unit": {"type": "string", "description": "Shown with values, e.g. '$', '%', 'h'"},
              "stacked": {"type": "boolean", "description": "Stack bar/area series"},
          }, "required": ["kind", "series"]}

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(PANEL_TYPES)},
        "title": {"type": "string", "description": "Optional panel heading"},
        "span": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Columns this panel spans"},
        "stats": _STATS, "table": _TABLE, "items": _ITEMS, "chart": _CHART, "graph": _GRAPH,
        "text": {"type": "string", "description": "Plain text, a few short paragraphs, no markdown"},
    },
    "required": ["type"],
}

SHOW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short window title, e.g. 'IBKR Portfolio'."},
        "subtitle": {"type": "string", "description": "Optional: account, date range, or 'as of 14:32 ET'."},
        "size": {"type": "string", "enum": sorted(SIZES),
                 "description": "small: a few numbers or a short list; medium: one table/chart; large: several "
                                "panels or a big graph; full: a dashboard. Deval can resize it."},
        "columns": {"type": "integer", "minimum": 1, "maximum": 3,
                    "description": "Panels flow into this many columns (default 1; 2 for side-by-side)."},
        "panels": {"type": "array", "items": PANEL_SCHEMA,
                   "description": "What the window shows, in order. Mix types freely."},
        "note": {"type": "string", "description": "One line at the bottom: source, caveats, data time."},
        "replace": {"type": "string", "description": "Id of an open window to update instead of opening a new one."},
        # Shortcuts for a single-section window (kept for older calls):
        "stats": _STATS, "table": _TABLE, "items": _ITEMS, "graph": _GRAPH,
        "text": {"type": "string"},
    },
    "required": ["title"],
}
CLOSE_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "Window id, or 'all'."}},
    "required": ["id"],
}


def _text(v: Any, limit: int = MAX_TEXT) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if v is not None else ""
    return s[:limit]


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    if isinstance(v, str):  # "$1,234.5" / "-3%" from a careless call
        try:
            f = float(v.replace(",", "").replace("$", "").replace("%", "").strip())
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def _clean_stats(stats) -> list[dict] | None:
    stats = [s for s in (stats or []) if isinstance(s, dict)][:MAX_STATS]
    return [{"label": _text(s.get("label"), 40), "value": _text(s.get("value"), 40),
             "tone": s.get("tone") if s.get("tone") in TONES else "neutral"} for s in stats] or None


def _clean_table(table) -> dict | None:
    if not (isinstance(table, dict) and table.get("columns")):
        return None
    cols = [_text(c, 40) for c in table["columns"][:MAX_COLS]]
    rows = [[_text(c, 120) for c in (r if isinstance(r, list) else [r])[:len(cols)]]
            for r in (table.get("rows") or [])[:MAX_ROWS]]
    return {"columns": cols, "rows": rows}


def _clean_items(items) -> list[dict] | None:
    items = [i for i in (items or []) if isinstance(i, dict)][:MAX_ITEMS]
    return [{"when": _text(i.get("when"), 60), "title": _text(i.get("title"), 160),
             "detail": _text(i.get("detail"), 300)} for i in items] or None


def _clean_graph(graph) -> dict | None:
    if not (isinstance(graph, dict) and graph.get("nodes")):
        return None
    nodes, seen = [], set()
    for n in graph["nodes"][:MAX_NODES]:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        nid = _text(n["id"], 80)
        if nid in seen:
            continue
        seen.add(nid)
        size = _num(n.get("size"))
        nodes.append({"id": nid, "label": _text(n.get("label") or nid, 120), "group": _text(n.get("group"), 30),
                      "detail": _text(n.get("detail"), 400), "size": int(max(1, min(5, size))) if size else 2})
    edges = [{"from": _text(e["from"], 80), "to": _text(e["to"], 80), "label": _text(e.get("label"), 40)}
             for e in (graph.get("edges") or [])[:MAX_EDGES]
             if isinstance(e, dict) and e.get("from") in seen and e.get("to") in seen]
    return {"nodes": nodes, "edges": edges} if nodes else None


def _clean_chart(chart) -> dict | None:
    if not isinstance(chart, dict) or chart.get("kind") not in CHART_KINDS:
        return None
    series = []
    for s in (chart.get("series") or [])[:MAX_SERIES]:
        if not isinstance(s, dict):
            continue
        values = [_num(v) for v in (s.get("values") or [])[:MAX_POINTS]]
        if any(v is not None for v in values):
            series.append({"name": _text(s.get("name"), 40), "values": values})
    if not series:
        return None
    n = max(len(s["values"]) for s in series)
    x = [_text(v, 40) for v in (chart.get("x") or [])[:MAX_POINTS]]
    x = (x + [str(i + 1) for i in range(len(x), n)])[:n]
    return {"kind": chart["kind"], "x": x, "series": series, "unit": _text(chart.get("unit"), 6),
            "stacked": bool(chart.get("stacked"))}


def _clean_image(img) -> dict | None:
    """Only inline images Jarvis made itself (browser screenshots): never a web
    address, which would make Deval's browser contact that site."""
    if not isinstance(img, dict):
        return None
    src = img.get("src") or ""
    if not (isinstance(src, str) and src.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))
            and len(src) <= MAX_IMAGE_CHARS):
        return None
    return {"src": src, "caption": _text(img.get("caption"), 200)}


CLEANERS = {"image": _clean_image, "stats": _clean_stats, "table": _clean_table, "items": _clean_items,
            "chart": _clean_chart, "graph": _clean_graph}


def _clean_panel(p: dict) -> dict | None:
    kind = p.get("type")
    if kind not in PANEL_TYPES:
        return None
    if kind == "text":
        body = _text(p.get("text"), 3000) or None
    else:
        body = CLEANERS[kind](p.get(kind))
    if body is None:
        return None
    out = {"type": kind, kind: body}
    if p.get("title"):
        out["title"] = _text(p["title"], 60)
    span = _num(p.get("span"))
    if span:
        out["span"] = int(max(1, min(3, span)))
    return out


def clean_window(args: dict) -> dict:
    """Keep only the known fields, as bounded plain data, with everything as panels."""
    win: dict = {"title": _text(args.get("title"), 80) or "Jarvis"}
    if args.get("subtitle"):
        win["subtitle"] = _text(args["subtitle"], 120)
    panels = [_clean_panel(p) for p in (args.get("panels") or []) if isinstance(p, dict)]
    # shortcut fields from a single-section call, in a sensible order
    for kind in ("stats", "text", "table", "items", "graph"):
        if args.get(kind) is not None:
            panels.append(_clean_panel({"type": kind, kind: args[kind]}))
    win["panels"] = [p for p in panels if p][:MAX_PANELS]
    has_graph_or_chart = any(p["type"] in ("graph", "chart") for p in win["panels"])
    size = args.get("size")
    win["size"] = size if size in SIZES else ("large" if has_graph_or_chart or len(win["panels"]) > 2 else "medium")
    cols = _num(args.get("columns"))
    win["columns"] = int(max(1, min(3, cols))) if cols else 1
    if args.get("note"):
        win["note"] = _text(args["note"], 300)
    return win


class WindowBoard:
    """The windows currently open on Deval's screen."""

    def __init__(self, on_change: OnChange | None = None):
        self.windows: dict[str, dict] = {}
        self.on_change = on_change

    async def _emit(self, event: dict) -> None:
        if self.on_change is not None:
            await self.on_change(event)

    async def show(self, args: dict) -> dict:
        win = clean_window(args)
        wid = args.get("replace") if args.get("replace") in self.windows else uuid.uuid4().hex[:8]
        win.update(id=wid, updated_at=utc_now())
        self.windows.pop(wid, None)
        self.windows[wid] = win
        while len(self.windows) > MAX_WINDOWS:  # the oldest goes
            old = next(iter(self.windows))
            self.windows.pop(old)
            await self._emit({"type": "window_closed", "id": old})
        await self._emit({"type": "window", "window": win})
        return win

    async def close(self, wid: str) -> list[str]:
        ids = list(self.windows) if wid == "all" else [wid] if wid in self.windows else []
        for i in ids:
            self.windows.pop(i)
            await self._emit({"type": "window_closed", "id": i})
        return ids

    def list(self) -> list[dict]:
        return list(self.windows.values())


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def build_display_server(board: WindowBoard):
    @tool("show_window",
          "Open (or update) a window on Deval's screen. You decide how to show the answer: pick the size, the "
          "number of columns, and a list of panels, mixing types freely: stats (headline numbers), table, "
          "items (timeline), chart (line/area over time, bar to compare amounts, pie/donut for shares), graph "
          "(how things connect), text (explanation). Fetch the data first. Then reply with one short spoken "
          "sentence, not the window's contents.",
          SHOW_SCHEMA)
    async def show_window(args: dict) -> dict:
        win = await board.show(args)
        if not win["panels"]:
            return _ok(f"Window {win['id']} opened but has nothing to show: every panel was empty or malformed.")
        kinds = ", ".join(p["type"] for p in win["panels"])
        return _ok(f"Window {win['id']} is on screen ({win['size']}; {kinds}). Don't read it out; summarise "
                   f"it in one sentence. To change it later, call show_window with replace='{win['id']}'.")

    @tool("close_window", "Close a window on Deval's screen by id, or all of them with id='all'.", CLOSE_SCHEMA)
    async def close_window(args: dict) -> dict:
        closed = await board.close(str(args.get("id", "")))
        return _ok(f"Closed {len(closed)} window(s)." if closed else "No such window is open.")

    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[show_window, close_window])
