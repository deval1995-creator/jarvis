"""The memory tools Claude sees, as an in-process MCP server.

In-process (create_sdk_mcp_server) rather than a separate stdio server: it
shares the Core's database connection, and the Core sees every call.
Tool names as Claude sees them: mcp__memory__<name>.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from core import memory

SERVER_NAME = "memory"
TOOL_NAMES = ["get_profile", "recall", "remember", "update", "forget", "log"]
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES]

OnProposal = Callable[[dict], Awaitable[None]]


def _ok(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


PENDING_NOTE = (
    "Proposed, not saved yet. It appears in Deval's memory inbox and takes effect only "
    "when Deval approves it. Tell Deval it's waiting for approval."
)


def build_memory_server(
    conn,
    context: Callable[[], dict],
    on_proposal: OnProposal | None = None,
):
    """`context()` returns {"conversation_id", "source"} for the current turn."""

    async def _propose(action: str, **kwargs) -> dict:
        try:
            ctx = context()
            p = memory.propose(
                conn,
                action,
                conversation_id=ctx.get("conversation_id"),
                source=ctx.get("source"),
                **kwargs,
            )
        except memory.MemoryError as e:
            return _err(str(e))
        if on_proposal is not None:
            await on_proposal(p)
        return _ok({"proposal_id": p["id"], "status": "pending", "note": PENDING_NOTE})

    @tool(
        "get_profile",
        "Deval's key facts: pinned and important memories plus known people. Call it at the start "
        "of a conversation when personal context would help.",
        {},
    )
    async def get_profile(args):
        return _ok(memory.get_profile(conn))

    @tool(
        "recall",
        "Search Deval's saved memories (people, places, preferences, decisions, past facts). "
        "Use before answering anything that might depend on what Deval told you before.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'wife name' or 'trading setup'"},
                "category": {"type": "string", "description": "Optional: trading, study, career, projects, home..."},
            },
            "required": ["query"],
        },
    )
    async def recall(args):
        results = memory.recall(conn, args["query"], category=args.get("category"))
        return _ok({"memories": results} if results else {"memories": [], "note": "Nothing saved matches."})

    @tool(
        "remember",
        "Propose saving a new long-term memory. Deval must approve it before it is stored. "
        "Write the text as a standalone fact, e.g. \"Deval prefers morning meetings.\" Set confidence below 1 "
        "for things you inferred rather than were told.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": list(memory.KINDS)},
                "category": {"type": "string"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                "confidence": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "people": {"type": "array", "items": {"type": "string"}, "description": "Names this is about"},
                "reason": {"type": "string", "description": "One line: why this is worth keeping"},
            },
            "required": ["text"],
        },
    )
    async def remember(args):
        return await _propose(
            "remember",
            text=args["text"],
            kind=args.get("kind"),
            category=args.get("category"),
            importance=args.get("importance"),
            confidence=args.get("confidence"),
            people=args.get("people"),
            reason=args.get("reason"),
        )

    @tool(
        "update",
        "Propose replacing an existing memory (by id from recall) with corrected text. Needs Deval's approval.",
        {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["memory_id", "text"],
        },
    )
    async def update(args):
        return await _propose("update", memory_id=args["memory_id"], text=args["text"], reason=args.get("reason"))

    @tool(
        "forget",
        "Propose removing a memory (by id from recall). Needs Deval's approval.",
        {
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["memory_id"],
        },
    )
    async def forget(args):
        return await _propose("forget", memory_id=args["memory_id"], reason=args.get("reason"))

    @tool(
        "log",
        "Propose a journal entry (what happened, how a day or trade went). Needs Deval's approval.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}, "category": {"type": "string"}},
            "required": ["text"],
        },
    )
    async def log(args):
        return await _propose("remember", text=args["text"], kind="journal", category=args.get("category"), importance=2)

    tools = [get_profile, recall, remember, update, forget, log]
    assert [t.name for t in tools] == TOOL_NAMES
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
