"""The Claude side of a session.

`Backend` is the narrow interface the session manager needs, so tests can swap
in a fake and never spend Pro usage. `SDKBackend` is the real one: a single
long-lived ClaudeSDKClient, resumed from a stored session id after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

log = logging.getLogger(__name__)

JARVIS_DIR = Path(__file__).resolve().parent.parent


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


def slim_options(
    resume: str | None = None,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> ClaudeAgentOptions:
    """The everyday chat session: no built-in tools, no claude.ai connectors, only
    the MCP servers the Core hands it (memory, for now).

    Measured on this PC: ~1k context tokens per turn instead of 20-28k, and first
    token in ~0.7 s instead of ~1.5 s. Tools get added one at a time.
    """
    return ClaudeAgentOptions(
        cwd=str(JARVIS_DIR),
        setting_sources=["project"],  # loads CLAUDE.md
        tools=[],
        mcp_servers=mcp_servers or {},
        allowed_tools=allowed_tools or [],  # anything not listed is refused
        strict_mcp_config=True,
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        include_partial_messages=True,
        resume=resume,
    )


class SDKBackend:
    def __init__(
        self,
        resume: str | None = None,
        mcp_servers: dict | None = None,
        allowed_tools: list[str] | None = None,
    ):
        self._resume = resume
        self._mcp_servers = mcp_servers
        self._allowed_tools = allowed_tools
        self._client: ClaudeSDKClient | None = None

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        return slim_options(resume, self._mcp_servers, self._allowed_tools)

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
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
            self._client = client
        return self._client

    async def turn(self, text: str) -> AsyncIterator[TurnEvent]:
        client = await self._ensure_client()
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
