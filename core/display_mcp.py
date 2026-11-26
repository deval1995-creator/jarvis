"""Windows Jarvis can open on Deval's screen, as an in-process MCP server.

Claude fetches the data (IBKR portfolio, calendar, anything) with its other
tools, then calls `show_window` with a structured description: headline stats,
a table, a list of timed items and a note. The Core keeps the open windows
and broadcasts them; the page draws them as floating HUD panels. Everything
is plain text on the page (never HTML), so a window can't run code.

Tool names as Claude sees them: mcp__display__show_window, mcp__display__close_window.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from core.db import utc_now

SERVER_NAME = "display"
TOOL_NAMES = ["show_window", "close_window"]
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES]

MAX_WINDOWS = 6
MAX_ROWS, MAX_COLS, MAX_ITEMS, MAX_STATS, MAX_TEXT = 200, 12, 100, 8, 600
TONES = {"up", "down", "warn", "neutral"}

OnChange = Callable[[dict], Awaitable[None]]

SHOW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short window title, e.g. 'IBKR Portfolio' or 'Calendar · Today'."},
        "subtitle": {"type": "string", "description": "Optional: account, date range, or 'as of 14:32 ET'."},
        "stats": {
            "type": "array", "description": "Headline numbers shown large (up to 8).",
            "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "value": {"type": "string"},
                "tone": {"type": "string", "enum": sorted(TONES), "description": "up = gain/green, down = loss/red"},
            }, "required": ["label", "value"]},
        },
        "table": {
            "type": "object", "description": "A table, e.g. positions. Cells are strings; start gains with '+' and losses with '-'.",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            }, "required": ["columns", "rows"],
        },
        "items": {
            "type": "array", "description": "A timeline, e.g. calendar events.",
            "items": {"type": "object", "properties": {
                "when": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"},
            }, "required": ["title"]},
        },
        "note": {"type": "string", "description": "One line at the bottom: source, caveats, data time."},
        "replace": {"type": "string", "description": "Id of an open window to update instead of opening a new one."},
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


def clean_window(args: dict) -> dict:
    """Keep only the known fields, as bounded plain text."""
    win: dict = {"title": _text(args.get("title"), 80) or "Jarvis"}
    if args.get("subtitle"):
        win["subtitle"] = _text(args["subtitle"], 120)
    stats = [s for s in (args.get("stats") or []) if isinstance(s, dict)][:MAX_STATS]
    if stats:
        win["stats"] = [{"label": _text(s.get("label"), 40), "value": _text(s.get("value"), 40),
                         "tone": s.get("tone") if s.get("tone") in TONES else "neutral"} for s in stats]
    table = args.get("table")
    if isinstance(table, dict) and table.get("columns"):
        cols = [_text(c, 40) for c in table["columns"][:MAX_COLS]]
        rows = [[_text(c, 120) for c in (r if isinstance(r, list) else [r])[:len(cols)]]
                for r in (table.get("rows") or [])[:MAX_ROWS]]
        win["table"] = {"columns": cols, "rows": rows}
    items = [i for i in (args.get("items") or []) if isinstance(i, dict)][:MAX_ITEMS]
    if items:
        win["items"] = [{"when": _text(i.get("when"), 60), "title": _text(i.get("title"), 160),
                         "detail": _text(i.get("detail"), 300)} for i in items]
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
    @tool("show_window", "Open (or update) a window on Deval's screen showing structured data: "
          "portfolio, calendar, lists, comparisons. Fetch the data first; put numbers in stats/table, "
          "events in items. Then reply with one short spoken sentence, not the window's contents.",
          SHOW_SCHEMA)
    async def show_window(args: dict) -> dict:
        win = await board.show(args)
        return _ok(f"Window {win['id']} is on screen. Don't read it out; summarise it in one sentence. "
                   f"To change it later, call show_window with replace='{win['id']}'.")

    @tool("close_window", "Close a window on Deval's screen by id, or all of them with id='all'.", CLOSE_SCHEMA)
    async def close_window(args: dict) -> dict:
        closed = await board.close(str(args.get("id", "")))
        return _ok(f"Closed {len(closed)} window(s)." if closed else "No such window is open.")

    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[show_window, close_window])
