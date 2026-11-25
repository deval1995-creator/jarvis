"""Core API: local-only HTTP + WebSocket.

Security (from the doc's checklist):
- Binds to 127.0.0.1 only (see core/__main__.py).
- Host header must be 127.0.0.1 or localhost, which blocks DNS-rebinding attacks.
- Every API call needs the local token (generated once, kept in the data dir).
- WebSocket connections from a browser must come from the Core's own page;
  non-browser clients (the voice service) send no Origin and rely on the token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core import db, store
from core.sessions import SOURCES, BackendFactory, SessionManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765


def load_or_create_token(data_dir: Path) -> str:
    path = data_dir / "api_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    source: str = "typed"


class Hub:
    """Fans turn events out to every connected window, so they stay in sync."""

    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()

    async def broadcast(self, event: dict) -> None:
        for ws in list(self.sockets):
            try:
                await ws.send_json(event)
            except Exception:
                self.sockets.discard(ws)


def create_app(
    conn=None,
    backend_factory: BackendFactory | None = None,
    token: str | None = None,
    port: int = DEFAULT_PORT,
) -> FastAPI:
    if conn is None:
        conn = db.connect()
    if backend_factory is None:
        from core.backend import SDKBackend

        backend_factory = SDKBackend
    if token is None:
        token = load_or_create_token(db.default_db_path().parent)

    manager = SessionManager(conn, backend_factory)
    hub = Hub()
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    turn_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for task in turn_tasks:
            task.cancel()
        await manager.close()

    app = FastAPI(title="Jarvis Core", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.manager = manager

    def require_token(x_jarvis_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_jarvis_token, token):
            raise HTTPException(status_code=401, detail="bad or missing X-Jarvis-Token")

    async def run_turn(text: str, source: str) -> dict:
        """Run a turn, broadcasting every event; returns the final event."""
        final: dict = {}
        async for event in manager.process_message(text, source):
            await hub.broadcast(event)
            final = event
        return final

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        # The page carries the token. Other websites can't read this response
        # (no CORS headers) and can't reach it by DNS rebinding (Host check).
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("__JARVIS_TOKEN__", json.dumps(token)[1:-1])

    @app.post("/api/message", dependencies=[Depends(require_token)])
    async def process_message(body: MessageIn) -> dict:
        if body.source not in SOURCES:
            raise HTTPException(status_code=422, detail=f"source must be one of {SOURCES}")
        return await run_turn(body.text, body.source)

    @app.post("/api/interrupt", dependencies=[Depends(require_token)])
    async def interrupt() -> dict:
        return {"interrupted": await manager.interrupt()}

    @app.get("/api/conversations", dependencies=[Depends(require_token)])
    async def list_conversations() -> list[dict]:
        return store.list_conversations(conn)

    @app.get("/api/conversations/current", dependencies=[Depends(require_token)])
    async def current_conversation() -> dict:
        return store.get_conversation(conn, manager.conversation_id)

    @app.get("/api/conversations/{conversation_id}", dependencies=[Depends(require_token)])
    async def get_conversation(conversation_id: str) -> dict:
        conversation = store.get_conversation(conn, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        return conversation

    @app.post("/api/conversations/new", dependencies=[Depends(require_token)])
    async def new_conversation() -> dict:
        conversation_id = await manager.new_conversation()
        await hub.broadcast({"type": "new_conversation", "conversation_id": conversation_id})
        return {"conversation_id": conversation_id}

    @app.get("/api/status", dependencies=[Depends(require_token)])
    async def get_status() -> dict:
        return manager.status()

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            await ws.close(code=4403)
            return
        await ws.accept()
        # The token comes in the first message, not the URL, so it never lands
        # in access logs.
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4401)
            return
        if not (
            isinstance(hello, dict)
            and hello.get("type") == "auth"
            and secrets.compare_digest(str(hello.get("token", "")), token)
        ):
            await ws.close(code=4401)
            return
        await ws.send_json({"type": "ready", "conversation_id": manager.conversation_id})
        hub.sockets.add(ws)
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "message":
                    text = str(msg.get("text", "")).strip()
                    source = msg.get("source", "typed")
                    if not text or source not in SOURCES:
                        await ws.send_json({"type": "error", "error": "need text and a valid source"})
                        continue
                    # Run in the background so this socket can still send an
                    # interrupt, or a new message that cuts the reply off.
                    task = asyncio.create_task(run_turn(text, source))
                    turn_tasks.add(task)
                    task.add_done_callback(turn_tasks.discard)
                elif kind == "interrupt":
                    await manager.interrupt()
                else:
                    await ws.send_json({"type": "error", "error": f"unknown type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            hub.sockets.discard(ws)

    return app
