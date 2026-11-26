"""The voice service: a small local web server that runs a Pipecat pipeline for
each browser (later, desktop app) connection over WebRTC.

    python -m voice        -> http://127.0.0.1:8766

The models load once at startup and are shared by every connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from voice import cuda

cuda.add_dll_directories()  # before faster-whisper / ctranslate2 load

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from core import db  # noqa: E402
from voice import models  # noqa: E402
from voice.core_client import CoreClient, read_token  # noqa: E402
from voice.pipeline import MODES, PTT  # noqa: E402

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
CORE_URL = os.environ.get("JARVIS_CORE_WS", "ws://127.0.0.1:8765/ws")
CORE_PAGE_ORIGINS = ("http://127.0.0.1:8765", "http://localhost:8765")


def create_app(port: int = 8766, core_url: str = CORE_URL, preload: bool = True) -> FastAPI:
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )

    from pipecat.workers.runner import WorkerRunner

    from voice.bot import build_task

    handler = SmallWebRTCRequestHandler()
    # Its own test page, and the Core's chat page (which the desktop app shows).
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", *CORE_PAGE_ORIGINS}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if preload:
            await asyncio.to_thread(models.preload)
        yield
        await handler.close()

    app = FastAPI(title="Jarvis voice", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    # The chat page calls /api/offer from another port, so it needs CORS; no other site does.
    app.add_middleware(CORSMiddleware, allow_origins=list(CORE_PAGE_ORIGINS), allow_methods=["POST"],
                       allow_headers=["Content-Type"])

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise HTTPException(status_code=403, detail="origin not allowed")

    async def run_bot(connection, mode: str) -> None:
        core = CoreClient(core_url, read_token(db.default_db_path().parent))
        try:
            await core.connect()
        except Exception:
            log.exception("voice: can't reach the Core at %s", core_url)
            await connection.disconnect()
            return
        task = build_task(connection, core, mode)
        try:
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(task)
            await runner.run()
        finally:
            await core.close()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/api/offer")
    async def offer(request: Request, background: BackgroundTasks) -> dict:
        check_origin(request)
        body = await request.json()
        data = body.get("request_data") or {}
        mode = data.get("mode", PTT)
        if mode not in MODES:
            raise HTTPException(status_code=422, detail=f"mode must be one of {MODES}")

        async def on_connection(connection) -> None:
            background.add_task(run_bot, connection, mode)

        return await handler.handle_web_request(
            request=SmallWebRTCRequest.from_dict(body), webrtc_connection_callback=on_connection
        )

    return app
