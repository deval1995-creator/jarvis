"""A stand-in for Claude, so tests never spend Pro usage."""

from __future__ import annotations

import asyncio

from core.backend import TurnResult


class FakeBackend:
    instances: list["FakeBackend"] = []

    def __init__(self, resume: str | None = None, chunks=("Hello", ", Deval."), delay: float = 0.0):
        self.resume = resume
        self.session_id = resume or f"fake-session-{len(FakeBackend.instances) + 1}"
        self.chunks = chunks
        self.delay = delay
        self.prompts: list[str] = []
        self.closed = False
        self._interrupted = False
        FakeBackend.instances.append(self)

    async def turn(self, text: str):
        self.prompts.append(text)
        self._interrupted = False
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            if self._interrupted:
                break
            yield ("delta", chunk)
        yield (
            "result",
            TurnResult(
                session_id=self.session_id,
                is_error=self._interrupted,
                usage={"input_tokens": 3, "output_tokens": 7, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 40},
                cost_usd=0.0,
            ),
        )

    async def interrupt(self) -> None:
        self._interrupted = True

    async def close(self) -> None:
        self.closed = True


class FailingBackend(FakeBackend):
    async def turn(self, text: str):
        yield ("delta", "Partial")
        raise ConnectionError("CLI process exited")
