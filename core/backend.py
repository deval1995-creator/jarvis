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


# A turn yields ("delta", str) for each piece of reply text, then exactly one
# ("result", TurnResult).
TurnEvent = tuple[str, "str | TurnResult"]


class Backend(Protocol):
    async def turn(self, text: str) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


def slim_options(resume: str | None = None) -> ClaudeAgentOptions:
    """The everyday chat session: no tools, no MCP servers, no claude.ai connectors.

    Measured on this PC: ~1k context tokens per turn instead of 20-28k, and first
    token in ~0.7 s instead of ~1.5 s. Tools get added one at a time in Week 2.
    """
    return ClaudeAgentOptions(
        cwd=str(JARVIS_DIR),
        setting_sources=["project"],  # loads CLAUDE.md
        tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        include_partial_messages=True,
        resume=resume,
    )


class SDKBackend:
    def __init__(self, resume: str | None = None):
        self._resume = resume
        self._client: ClaudeSDKClient | None = None

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            client = ClaudeSDKClient(options=slim_options(self._resume))
            try:
                await client.connect()
            except Exception:
                if not self._resume:
                    raise
                # The stored session may be gone (deleted, or a different machine).
                # Start fresh rather than refusing to talk.
                log.warning("could not resume session %s; starting a new one", self._resume)
                self._resume = None
                client = ClaudeSDKClient(options=slim_options())
                await client.connect()
            self._client = client
        return self._client

    async def turn(self, text: str) -> AsyncIterator[TurnEvent]:
        client = await self._ensure_client()
        streamed = False
        try:
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            streamed = True
                            yield ("delta", delta["text"])
                elif isinstance(msg, AssistantMessage) and not streamed:
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
