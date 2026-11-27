"""The Claude side of a session.

`Backend` is the narrow interface the session manager needs, so tests can swap
in a fake and never spend Pro usage. `SDKBackend` is the real one: a single
long-lived ClaudeSDKClient, resumed from a stored session id after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from core import db
from core.connectors import WEB_TOOLS

log = logging.getLogger(__name__)

# Allowed tools skip can_use_tool by design: it is the catch-all that refuses
# everything *not* allowed. The SDK warns about that; it's intended here.
warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")

JARVIS_DIR = Path(__file__).resolve().parent.parent
PERSONA_SOURCE = JARVIS_DIR / "jarvis_home" / "CLAUDE.md"  # edited in the repo
# Jarvis's Claude Code home: its persona and its own connector choices. It must
# be outside the git repo, because Claude Code keys per-project settings (like
# switched-off connectors) by repository root; inside the repo they would also
# apply to dev sessions in C:\Jarvis.
AGENT_HOME = db.default_db_path().parent / "home"
# Sonnet 5.5: about 3-4x faster to first words than Opus 5.5 in Jarvis's tests, and
# strong enough for tools and window design. JARVIS_MODEL overrides it.
MODEL = os.environ.get("JARVIS_MODEL", "claude-sonnet-5-5")


def known_facts() -> str:
    """Deval's key memories (pinned and important, local-only left out), so a
    fresh session knows the basics without a recall step."""
    try:
        from core import memory

        conn = db.connect()
        try:
            facts = [m["text"] for m in memory.get_profile(conn, limit=25)["memories"]]
        finally:
            conn.close()
    except Exception:
        log.debug("could not read memories for the persona", exc_info=True)
        return ""
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return ("\n\n## What you already know about Deval (from memory, as this conversation began)\n"
            "Use these directly; call `recall` for anything else or more detail.\n" + lines + "\n")


def prepare_agent_home() -> Path:
    """Create Jarvis's home and copy the current persona (plus key memories) into it."""
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    target = AGENT_HOME / "CLAUDE.md"
    persona = PERSONA_SOURCE.read_text(encoding="utf-8") + known_facts()
    if not target.exists() or target.read_text(encoding="utf-8") != persona:
        target.write_text(persona, encoding="utf-8")
    return AGENT_HOME

# before_tool(tool_name, tool_input, tool_use_id, session_id)
ToolHook = Callable[[str, dict, "str | None", "str | None"], Awaitable[None]]
# after_tool(tool_use_id, response, error)
AfterToolHook = Callable[["str | None", object, "str | None"], Awaitable[None]]


@dataclass
class TurnResult:
    session_id: str | None
    is_error: bool = False
    error: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None


# A turn yields ("delta", str) for each piece of reply text, ("tool", name) when
# Claude calls a tool, then exactly one ("result", TurnResult).
TurnEvent = tuple[str, "str | TurnResult"]


class Backend(Protocol):
    async def turn(self, text: str) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


def session_options(
    resume: str | None = None,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
    connectors: bool = False,
    before_tool: ToolHook | None = None,
    after_tool: AfterToolHook | None = None,
    can_use_tool=None,
) -> ClaudeAgentOptions:
    """The chat session: no built-in tools (files, shell, web), Jarvis's own MCP
    servers, and optionally the claude.ai connectors (read-only Gmail + Calendar).

    Without connectors a turn reads ~1k context tokens; connectors add their tool
    definitions to every turn, so the Core switches off all but the two it uses.
    """
    options = ClaudeAgentOptions(
        cwd=str(prepare_agent_home()),
        setting_sources=["project"],  # loads the persona copied from jarvis_home/CLAUDE.md
        # Built-in tools: only web search and page reading. No files, shell or code.
        tools=list(WEB_TOOLS),
        mcp_servers=mcp_servers or {},
        allowed_tools=list(allowed_tools or []) + list(WEB_TOOLS),
        include_partial_messages=True,
        resume=resume,
        model=MODEL,
    )
    if connectors:
        from core import connectors as conn_policy

        options.allowed_tools += conn_policy.ALLOWED_TOOLS
        options.disallowed_tools = list(conn_policy.DISALLOWED_TOOLS)
        # Approval tools and anything unlisted come here; the default refuses all.
        options.can_use_tool = can_use_tool or conn_policy.deny_unlisted
    else:
        options.strict_mcp_config = True
        options.env = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
        options.can_use_tool = None
    hooks: dict = {}
    if before_tool is not None:

        async def pre_tool_use(hook_input, tool_use_id, context):
            await before_tool(
                hook_input["tool_name"], hook_input.get("tool_input") or {},
                hook_input.get("tool_use_id") or tool_use_id, hook_input.get("session_id"),
            )
            return {}

        hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[pre_tool_use])]
    if after_tool is not None:

        async def post_tool_use(hook_input, tool_use_id, context):
            await after_tool(hook_input.get("tool_use_id") or tool_use_id, hook_input.get("tool_response"), None)
            return {}

        async def post_tool_failure(hook_input, tool_use_id, context):
            await after_tool(hook_input.get("tool_use_id") or tool_use_id, None, hook_input.get("error") or "failed")
            return {}

        hooks["PostToolUse"] = [HookMatcher(matcher=None, hooks=[post_tool_use])]
        hooks["PostToolUseFailure"] = [HookMatcher(matcher=None, hooks=[post_tool_failure])]
    if hooks:
        options.hooks = hooks
    return options


class SDKBackend:
    def __init__(
        self,
        resume: str | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
        connectors: bool = False,
        before_tool: ToolHook | None = None,
        after_tool: AfterToolHook | None = None,
        can_use_tool=None,
    ):
        self._after_tool = after_tool
        self._can_use_tool = can_use_tool
        self._resume = resume
        self._mcp_servers = mcp_servers
        self._allowed_tools = allowed_tools
        self._connectors = connectors
        self._before_tool = before_tool
        self._client: ClaudeSDKClient | None = None
        self._connect_lock = asyncio.Lock()
        self.connector_status: dict[str, str] | None = None

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        return session_options(
            resume, self._mcp_servers, self._allowed_tools, self._connectors,
            self._before_tool, self._after_tool, self._can_use_tool,
        )

    async def warm(self) -> dict[str, str] | None:
        """Start the session ahead of the first message, connectors loaded."""
        await self._ensure_client()
        return self.connector_status

    async def _ensure_client(self) -> ClaudeSDKClient:
        # warm() and the first turn can race; only one of them may connect.
        async with self._connect_lock:
            if self._client is None:
                self._client = await self._connect()
            return self._client

    async def _connect(self) -> ClaudeSDKClient:
        client = await self._open()
        if self._connectors:
            # Connectors load in the background after connect. Wait for them (and
            # re-apply the selection) so the first message has its tools.
            from core import connectors as conn_policy

            self.connector_status = await conn_policy.select_servers(client)
        return client

    async def _open(self) -> ClaudeSDKClient:
        client = ClaudeSDKClient(options=self._options(self._resume))
        try:
            await client.connect()
        except Exception:
            if not self._resume:
                raise
            # The stored session may be gone (deleted, or a different machine).
            # Start fresh rather than refusing to talk.
            log.warning("could not resume session %s; starting a new one", self._resume)
            self._resume = None
            client = ClaudeSDKClient(options=self._options(None))
            await client.connect()
        return client

    async def context_tokens(self) -> int | None:
        """How much the live session re-reads on every step (its context size)."""
        if self._client is None:
            return None
        try:
            return int((await self._client.get_context_usage()).get("totalTokens") or 0)
        except Exception:
            log.debug("context usage unavailable", exc_info=True)
            return None

    async def check_connectors(self) -> dict[str, str] | None:
        """Repair any connector that dropped out of the live session (see
        connectors.ensure_connected). Cheap when all is well."""
        if not self._connectors or self._client is None:
            return self.connector_status
        from core import connectors as conn_policy

        async with self._connect_lock:
            if self._client is not None:
                self.connector_status = await conn_policy.ensure_connected(self._client)
        return self.connector_status

    async def turn(self, text: str) -> AsyncIterator[TurnEvent]:
        client = await self._ensure_client()
        if self._connectors:
            try:  # every message starts with Gmail, Calendar and IBKR present
                await self.check_connectors()
            except Exception:
                log.exception("connector check failed; continuing with what's loaded")
        streamed = False  # any text streamed this turn
        need_break = False  # text resumes after a tool call: start a new paragraph
        saw_events = False
        try:
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    saw_events = True
                    event = msg.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if need_break:
                                yield ("delta", "\n\n")
                                need_break = False
                            streamed = True
                            yield ("delta", delta["text"])
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            yield ("tool", block.name)
                            need_break = streamed
                    if not saw_events:  # streaming unavailable: fall back to whole messages
                        text_out = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                        if text_out:
                            yield ("delta", text_out)
                elif isinstance(msg, ResultMessage):
                    self._resume = msg.session_id
                    yield (
                        "result",
                        TurnResult(
                            session_id=msg.session_id,
                            is_error=msg.is_error,
                            error="; ".join(msg.errors or []) or (msg.subtype if msg.is_error else None),
                            usage=msg.usage or {},
                            cost_usd=msg.total_cost_usd,
                        ),
                    )
        except Exception:
            # A broken CLI process can't be trusted for the next turn; reconnect then.
            await self.close()
            raise

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:  # already dead
                log.debug("disconnect failed", exc_info=True)
