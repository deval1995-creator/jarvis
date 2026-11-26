"""The voice service's line to the Core: one WebSocket, token in the first message.

The Core broadcasts every event (typed turns, memory, approvals) to every
socket. The voice pipeline picks out its own turns from that stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)


def read_token(data_dir: Path) -> str:
    return (data_dir / "api_token").read_text(encoding="utf-8").strip()


class CoreClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._ws: ClientConnection | None = None
        self.conversation_id: str | None = None

    async def connect(self) -> None:
        ws = await connect(self.url, max_size=None)
        await ws.send(json.dumps({"type": "auth", "token": self.token}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ready.get("type") != "ready":
            await ws.close()
            raise ConnectionError(f"Core refused the voice service: {ready}")
        self.conversation_id = ready.get("conversation_id")
        self._ws = ws

    async def send(self, message: dict) -> None:
        if self._ws is None:
            await self.connect()
        await self._ws.send(json.dumps(message))

    async def ask(self, text: str) -> None:
        await self.send({"type": "message", "text": text, "source": "voice"})

    async def interrupt(self) -> None:
        await self.send({"type": "interrupt"})

    async def events(self) -> AsyncIterator[dict]:
        """Every event the Core broadcasts, reconnecting if the Core restarts."""
        while True:
            try:
                if self._ws is None:
                    await self.connect()
                async for raw in self._ws:
                    yield json.loads(raw)
                raise ConnectionError("Core closed the connection")
            except (OSError, ConnectionError, asyncio.TimeoutError, WebSocketException) as e:
                log.warning("lost the Core (%s); retrying in 2 s", e)
                self._ws = None
                yield {"type": "core_disconnected"}
                await asyncio.sleep(2)

    async def close(self) -> None:
        if self._ws is not None:
            ws, self._ws = self._ws, None
            await ws.close()
