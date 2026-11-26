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
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core import approvals, audit, db, memory, memory_review, store
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


class ApproveIn(BaseModel):
    text: str | None = Field(default=None, max_length=5_000)  # optional edit before approving


class MemoryEditIn(BaseModel):
    text: str | None = Field(default=None, max_length=5_000)
    category: str | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    pinned: bool | None = None


class LocalOnlyIn(BaseModel):
    categories: list[str]


class AutoApproveIn(BaseModel):
    enabled: bool


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
    review_runner: memory_review.Runner | None = None,
    idle_review_seconds: float = 20 * 60,
    idle_check_seconds: float = 60,
    connectors: bool = True,
) -> FastAPI:
    if conn is None:
        conn = db.connect()
    if token is None:
        token = load_or_create_token(db.default_db_path().parent)
    hub = Hub()

    async def on_proposal(p: dict) -> None:
        kind = "memory_decided" if p["status"] != "pending" else "memory_proposal"
        await hub.broadcast({"type": kind, "proposal": p})

    from core.display_mcp import WindowBoard

    board = WindowBoard(on_change=hub.broadcast)  # windows Jarvis opens on the page

    if backend_factory is None:
        from core import display_mcp
        from core.backend import SDKBackend
        from core.memory_mcp import ALLOWED_TOOLS, build_memory_server

        display_server = display_mcp.build_display_server(board)

        # `manager` is bound below; the lambda reads it at call time.
        memory_server = build_memory_server(
            conn,
            context=lambda: {
                "conversation_id": manager.conversation_id,
                "source": manager.current_source,
                "untrusted": manager.untrusted,
            },
            on_proposal=on_proposal,
        )

        def backend_factory(resume):
            return SDKBackend(
                resume,
                mcp_servers={"memory": memory_server, "display": display_server},
                allowed_tools=ALLOWED_TOOLS + display_mcp.ALLOWED_TOOLS,
                connectors=connectors,
                before_tool=before_tool,
                after_tool=after_tool,
                can_use_tool=permission_gate,
            )

    # ----- tool calls: audit, untrusted flag, approvals (bound late, like manager) -----

    async def on_approval_event(event: dict) -> None:
        await hub.broadcast(event)

    broker = approvals.ApprovalBroker(conn, on_event=on_approval_event)

    async def before_tool(tool_name: str, tool_input: dict, tool_use_id, session_id) -> None:
        """Runs before every tool call (and before the permission check)."""
        from core.connectors import reads_outside_content

        audit.started(conn, tool=tool_name, arguments=tool_input, tool_use_id=tool_use_id,
                      session_id=session_id, conversation_id=manager.conversation_id)
        # Before the tool runs, so a memory call later in the same turn already
        # sees the conversation as untrusted.
        if reads_outside_content(tool_name) and not manager.untrusted:
            manager.mark_untrusted()
            await hub.broadcast({"type": "untrusted", "conversation_id": manager.conversation_id})

    async def after_tool(tool_use_id, response, error) -> None:
        audit.finished(conn, tool_use_id, result=response, error=error)

    async def permission_gate(tool_name: str, tool_input: dict, context):
        """Claude Code asks here for every tool that isn't plainly allowed."""
        from core.connectors import NEEDS_APPROVAL, deny_unlisted

        tool_use_id = getattr(context, "tool_use_id", None)
        if tool_name not in NEEDS_APPROVAL:
            audit.set_status(conn, tool_use_id, "denied")
            return await deny_unlisted(tool_name, tool_input, context)
        result = await broker.request(
            tool_name,
            tool_input,
            conversation_id=manager.conversation_id,
            untrusted=manager.untrusted,
            on_created=lambda approval_id: audit.set_status(conn, tool_use_id, "awaiting_approval", approval_id),
        )
        audit.set_status(conn, tool_use_id, "approved" if result.behavior == "allow" else "denied")
        return result

    manager = SessionManager(conn, backend_factory)
    background_tasks: set[asyncio.Task] = set()

    async def on_review(result: memory_review.ReviewResult) -> None:
        await hub.broadcast(
            {
                "type": "memory_review",
                "conversation_id": result.conversation_id,
                "saved": [p["text"] for p in result.saved],
                "pending": [p["text"] for p in result.pending],
            }
        )

    reviewer = memory_review.Reviewer(conn, review_runner or memory_review.sdk_runner, on_result=on_review)

    def review_in_background(conversation_id: str) -> None:
        task = asyncio.create_task(reviewer.review(conversation_id))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def idle_review_loop() -> None:
        """Review the current conversation once it has been quiet for a while."""
        from datetime import datetime, timezone

        while True:
            await asyncio.sleep(idle_check_seconds)
            try:
                if manager.busy or not memory_review.pending_review(conn, manager.conversation_id):
                    continue
                row = conn.execute(
                    "select max(created_at) from messages where conversation_id = ?", (manager.conversation_id,)
                ).fetchone()
                if row[0] is None:
                    continue
                quiet = datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
                if quiet.total_seconds() >= idle_review_seconds:
                    await reviewer.review(manager.conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("idle review check failed")
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    turn_tasks: set[asyncio.Task] = set()

    async def warm_up() -> None:
        warm = getattr(manager.backend, "warm", None)
        if warm is None:
            return
        try:
            status = await warm()
            if status:
                log.info("connectors: %s", status)
        except Exception:
            log.exception("warm-up failed; the first message will start the session instead")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        expired = broker.expire_orphans()
        pruned = audit.prune(conn)
        if expired or pruned:
            log.info("startup: expired %d stale approvals, pruned %d old audit rows", expired, pruned)
        watcher = asyncio.create_task(idle_review_loop())
        warmer = asyncio.create_task(warm_up())
        background_tasks.add(warmer)
        warmer.add_done_callback(background_tasks.discard)
        yield
        watcher.cancel()
        for task in turn_tasks | background_tasks:
            task.cancel()
        await manager.close()

    app = FastAPI(title="Jarvis Core", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.manager = manager
    app.state.broker = broker
    app.state.board = board
    app.state.tool_hooks = {"before": before_tool, "after": after_tool, "gate": permission_gate}

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

    def page(name: str) -> str:
        # The page carries the token. Other websites can't read this response
        # (no CORS headers) and can't reach it by DNS rebinding (Host check).
        html = (STATIC_DIR / name).read_text(encoding="utf-8")
        return html.replace("__JARVIS_TOKEN__", json.dumps(token)[1:-1])

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return page("index.html")

    SCRIPTS = {"voice.js", "orb.js"}

    @app.get("/static/{name}")
    async def script(name: str) -> Response:
        if name not in SCRIPTS:
            raise HTTPException(status_code=404)
        return Response((STATIC_DIR / name).read_text(encoding="utf-8"), media_type="text/javascript")

    @app.post("/api/message", dependencies=[Depends(require_token)])
    async def process_message(body: MessageIn) -> dict:
        if body.source not in SOURCES:
            raise HTTPException(status_code=422, detail=f"source must be one of {SOURCES}")
        return await run_turn(body.text, body.source)

    @app.post("/api/interrupt", dependencies=[Depends(require_token)])
    async def interrupt() -> dict:
        await broker.cancel_all()
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
        ended = manager.conversation_id
        await broker.cancel_all()
        conversation_id = await manager.new_conversation()
        review_in_background(ended)
        # Start the new session now so its connectors are ready for the first message.
        warmer = asyncio.create_task(warm_up())
        background_tasks.add(warmer)
        warmer.add_done_callback(background_tasks.discard)
        await hub.broadcast({"type": "new_conversation", "conversation_id": conversation_id})
        return {"conversation_id": conversation_id}

    # ----- memory: Deval's side (Claude can only propose) -----

    def _memory_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except memory.MemoryError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.get("/api/memory/proposals", dependencies=[Depends(require_token)])
    async def list_proposals(status: str = "pending") -> list[dict]:
        return memory.list_proposals(conn, status)

    @app.post("/api/memory/proposals/{proposal_id}/approve", dependencies=[Depends(require_token)])
    async def approve_proposal(proposal_id: int, body: ApproveIn | None = None) -> dict:
        p = _memory_call(memory.approve, conn, proposal_id, body.text if body else None)
        await hub.broadcast({"type": "memory_decided", "proposal": p})
        return p

    @app.post("/api/memory/proposals/{proposal_id}/reject", dependencies=[Depends(require_token)])
    async def reject_proposal(proposal_id: int) -> dict:
        p = _memory_call(memory.reject, conn, proposal_id)
        await hub.broadcast({"type": "memory_decided", "proposal": p})
        return p

    @app.get("/api/memory", dependencies=[Depends(require_token)])
    async def search_memory(q: str | None = None, archived: bool = False) -> list[dict]:
        return memory.list_memories(conn, q, include_archived=archived)

    @app.patch("/api/memory/{memory_id}", dependencies=[Depends(require_token)])
    async def edit_memory(memory_id: int, body: MemoryEditIn) -> dict:
        fields = body.model_dump(exclude_none=True)
        if "pinned" in fields:
            fields["pinned"] = int(fields["pinned"])
        return _memory_call(memory.edit_memory, conn, memory_id, **fields)

    @app.delete("/api/memory/{memory_id}", dependencies=[Depends(require_token)])
    async def delete_memory(memory_id: int) -> dict:
        _memory_call(memory.archive_memory, conn, memory_id)
        return {"archived": memory_id}

    @app.get("/api/memory/settings/local-only", dependencies=[Depends(require_token)])
    async def get_local_only() -> dict:
        return {"categories": memory.local_only_categories(conn)}

    @app.put("/api/memory/settings/local-only", dependencies=[Depends(require_token)])
    async def put_local_only(body: LocalOnlyIn) -> dict:
        memory.set_local_only_categories(conn, body.categories)
        return {"categories": memory.local_only_categories(conn)}

    @app.post("/api/memory/review", dependencies=[Depends(require_token)])
    async def review_now() -> dict:
        """Review the current conversation now instead of waiting for it to go quiet."""
        result = await reviewer.review(manager.conversation_id)
        if result is None:
            raise HTTPException(status_code=502, detail="review failed; see the Core log")
        return {
            "reviewed_messages": result.reviewed_messages,
            "saved": [p["text"] for p in result.saved],
            "pending": [p["text"] for p in result.pending],
            "skipped_duplicates": result.skipped_duplicates,
            "skipped_low_confidence": result.skipped_low_confidence,
            "summary": result.summary,
            "skipped_reason": result.skipped_reason,
        }

    @app.get("/api/memory/settings/auto-approve", dependencies=[Depends(require_token)])
    async def get_auto_approve() -> dict:
        return {"enabled": memory.auto_approve_enabled(conn)}

    @app.put("/api/memory/settings/auto-approve", dependencies=[Depends(require_token)])
    async def put_auto_approve(body: AutoApproveIn) -> dict:
        memory.set_auto_approve(conn, body.enabled)
        return {"enabled": memory.auto_approve_enabled(conn)}

    # ----- approvals and audit -----

    def _approval_call(coro_fn, *args):
        async def run():
            try:
                return await coro_fn(*args)
            except approvals.ApprovalError as e:
                raise HTTPException(status_code=409, detail=str(e))
        return run()

    @app.get("/api/approvals", dependencies=[Depends(require_token)])
    async def list_approvals(status: str = "pending") -> list[dict]:
        return broker.list(status)

    @app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(require_token)])
    async def approve_action(approval_id: str) -> dict:
        return await _approval_call(broker.decide, approval_id, True)

    @app.post("/api/approvals/{approval_id}/reject", dependencies=[Depends(require_token)])
    async def reject_action(approval_id: str) -> dict:
        return await _approval_call(broker.decide, approval_id, False)

    @app.get("/api/audit", dependencies=[Depends(require_token)])
    async def get_audit(limit: int = 100) -> list[dict]:
        return audit.recent(conn, min(limit, 1000))

    @app.get("/api/windows", dependencies=[Depends(require_token)])
    async def list_windows() -> list[dict]:
        return board.list()

    @app.delete("/api/windows/{window_id}", dependencies=[Depends(require_token)])
    async def close_window(window_id: str) -> dict:
        return {"closed": await board.close(window_id)}

    @app.get("/api/status", dependencies=[Depends(require_token)])
    async def get_status() -> dict:
        return {**manager.status(), "connectors": getattr(manager.backend, "connector_status", None)}

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
                    await broker.cancel_all()
                    await manager.interrupt()
                else:
                    await ws.send_json({"type": "error", "error": f"unknown type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            hub.sockets.discard(ws)

    return app
