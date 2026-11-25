"""End-to-end over a real uvicorn server and a real WebSocket client.

TestClient handles WebSockets itself, so it can't catch a server that lacks
WebSocket support. This test can.
"""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
import websockets

from core import db
from core.api import create_app
from tests.fakes import FakeBackend

TOKEN = "live-token"


async def no_claude_review(system_prompt, prompt):
    return {"memories": [], "summary": "test"}, None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = free_port()
    app = create_app(conn=db.connect(tmp_path / "jarvis.db"), backend_factory=FakeBackend, token=TOKEN, port=port, review_runner=no_claude_review)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield port
    srv.should_exit = True
    thread.join(timeout=5)


def test_real_websocket_turn(server):
    async def run():
        async with websockets.connect(
            f"ws://127.0.0.1:{server}/ws", origin=f"http://127.0.0.1:{server}"
        ) as ws:
            await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
            assert json.loads(await ws.recv())["type"] == "ready"
            await ws.send(json.dumps({"type": "message", "text": "hi"}))
            types = []
            while not types or types[-1] not in ("done", "interrupted", "error"):
                types.append(json.loads(await ws.recv())["type"])
            return types

    assert asyncio.run(run()) == ["start", "delta", "delta", "done"]
