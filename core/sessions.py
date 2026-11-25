"""The main session: one conversation, one turn at a time.

New input while Jarvis is replying interrupts that reply, the same way talking
over Jarvis will in the voice pipeline. Every message is saved before and after
the turn, and the Claude session id is stored so a restart resumes the chat.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from core import db, store
from core.backend import Backend, TurnResult

SOURCES = ("typed", "voice", "routine", "discord")

BackendFactory = Callable[[str | None], Backend]


@dataclass
class _Turn:
    interrupted: bool = False


class SessionManager:
    def __init__(self, conn, backend_factory: BackendFactory):
        self.conn = conn
        self._backend_factory = backend_factory
        row = store.active_conversation(conn)
        if row is None:
            self.conversation_id = store.create_conversation(conn)
            claude_session_id = None
        else:
            self.conversation_id = row["id"]
            claude_session_id = row["claude_session_id"]
        self.backend = backend_factory(claude_session_id)
        self._lock = asyncio.Lock()
        self._current: _Turn | None = None
        self.last_turn: dict | None = None

    @property
    def busy(self) -> bool:
        return self._current is not None

    async def interrupt(self) -> bool:
        """Stop the reply in progress, if any. Returns whether there was one."""
        turn = self._current
        if turn is None or turn.interrupted:
            return False
        turn.interrupted = True
        await self.backend.interrupt()
        return True

    async def process_message(self, text: str, source: str = "typed") -> AsyncIterator[dict]:
        """Run one turn, yielding events: start, delta*, then done | interrupted | error.

        Consume it to the end: the turn lock is held until the generator finishes.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        text = text.strip()
        if not text:
            raise ValueError("empty message")

        await self.interrupt()  # new input cuts off a reply in progress
        async with self._lock:
            turn = _Turn()
            self._current = turn
            try:
                async for event in self._run_turn(turn, text, source):
                    yield event
            finally:
                self._current = None

    async def _run_turn(self, turn: _Turn, text: str, source: str) -> AsyncIterator[dict]:
        conversation_id = self.conversation_id
        store.add_message(self.conn, conversation_id, "user", source, text)
        yield {"type": "start", "conversation_id": conversation_id, "source": source, "text": text}

        start = time.perf_counter()
        first_token_ms: int | None = None
        parts: list[str] = []
        result: TurnResult | None = None
        error: str | None = None
        try:
            async for kind, payload in self.backend.turn(text):
                if kind == "delta":
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - start) * 1000)
                    parts.append(payload)
                    yield {"type": "delta", "text": payload}
                elif kind == "result":
                    result = payload
        except Exception as exc:  # the CLI died, network dropped, limits hit...
            error = f"{type(exc).__name__}: {exc}"

        total_ms = int((time.perf_counter() - start) * 1000)
        reply = "".join(parts)
        if reply:
            # Saved even when cut short, so the record matches what you heard.
            store.add_message(self.conn, conversation_id, "assistant", source, reply)
        if result is not None:
            if result.session_id:
                store.set_claude_session_id(self.conn, conversation_id, result.session_id)
            db.record_usage(
                self.conn,
                kind="chat" if source != "routine" else "routine",
                claude_session_id=result.session_id,
                usage=result.usage,
                cost_usd=result.cost_usd,
                duration_ms=total_ms,
                first_token_ms=first_token_ms,
            )
            if result.is_error and not turn.interrupted and error is None:
                error = result.error or "Claude returned an error"

        self.last_turn = {"first_token_ms": first_token_ms, "total_ms": total_ms, "source": source}
        if turn.interrupted:
            yield {"type": "interrupted", "text": reply, **self.last_turn}
        elif error:
            yield {"type": "error", "error": error, "text": reply, **self.last_turn}
        else:
            yield {"type": "done", "text": reply, **self.last_turn}

    async def new_conversation(self) -> str:
        """End the current conversation and start a fresh Claude session."""
        await self.interrupt()
        async with self._lock:
            store.end_conversation(self.conn, self.conversation_id)
            await self.backend.close()
            self.conversation_id = store.create_conversation(self.conn)
            self.backend = self._backend_factory(None)
            return self.conversation_id

    def status(self) -> dict:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.conn.execute(
            "select claude_session_id from conversations where id = ?", (self.conversation_id,)
        ).fetchone()
        return {
            "conversation_id": self.conversation_id,
            "claude_session_id": row["claude_session_id"] if row else None,
            "busy": self.busy,
            "last_turn": self.last_turn,
            "usage_today_utc": store.usage_since(self.conn, today.isoformat(timespec="milliseconds")),
        }

    async def close(self) -> None:
        await self.backend.close()
