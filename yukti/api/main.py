"""
yukti/api/main.py
FastAPI application — serves the Flutter mobile dashboard and Grafana.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from yukti.api.routes.positions import (
    positions_router, pnl_router, trades_router,
    journal_router, control_router,
)
from yukti.metrics import metrics_response, agent_halted, signal_loop_last_run
from yukti.data.state import is_halted, get_performance_state, get_all_positions

log = logging.getLogger(__name__)

# ── WebSocket connection manager (Flutter live updates) ────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._active: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._active.add(ws)
        log.info("WS client connected (total=%d)", len(self._active))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._active.discard(ws)  # Safe, no error if not present
        log.info("WS client disconnected (total=%d)", len(self._active))

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        async with self._lock:
            active_copy = list(self._active)
        for ws in active_copy:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


manager = ConnectionManager()


async def _push_loop() -> None:
    """Background task — pushes live state to all Flutter WS clients every 5s."""
    while True:
        await asyncio.sleep(5)
        if not manager._active:
            continue
        try:
            halted = await is_halted()
            perf   = await get_performance_state()
            positions = await get_all_positions()
            await manager.broadcast({
                "type":      "state_update",
                "halted":    halted,
                "perf":      perf,
                "positions": positions,
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as exc:
            log.warning("WS push failed: %s", exc)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background task
    push_task = asyncio.create_task(_push_loop())
    app.state.push_task = push_task  # Store in app state
    log.info("Yukti API ready")
    yield
    # Cancel background task on shutdown
    push_task.cancel()
    try:
        await push_task
    except asyncio.CancelledError:
        log.info("Push task cancelled on shutdown")
    log.info("Yukti API shutdown")


# ── App construction ──────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title       = "Yukti Trading Agent",
        description = "NSE/BSE autonomous trading agent API",
        version     = "0.1.0",
        lifespan    = lifespan,
    )

    # CORS: strict allowlist, fail closed in live mode
    if settings.mode == "live":
        allow_origins = []  # No CORS in live mode for security
    else:
        allow_origins = settings.cors_allow_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins  = allow_origins,
        allow_methods  = ["GET", "POST", "PUT", "DELETE"],
        allow_headers  = ["Content-Type", "Authorization"],
    )

    # ── Routers ────────────────────────────────────────────────────────────────
    app.include_router(positions_router)
    app.include_router(pnl_router)
    app.include_router(trades_router)
    app.include_router(journal_router)
    app.include_router(control_router)

    # ── Core endpoints ─────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, Any]:
        halted = await is_halted()
        agent_halted.set(1 if halted else 0)
        return {
            "status":    "halted" if halted else "ok",
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Prometheus scrape endpoint."""
        body, ct = metrics_response()
        return Response(content=body, media_type=ct)

    # ── WebSocket for Flutter live dashboard ──────────────────────────────────

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        """
        Flutter connects here to receive real-time position and P&L updates.
        Messages are JSON with type "state_update".
        Flutter can also send {"type": "halt"} to trigger kill switch.
        """
        await manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                msg  = json.loads(data)
                if msg.get("type") == "halt":
                    from yukti.data.state import set_halt
                    await set_halt(True)
                    await websocket.send_json({"type": "ack", "halted": True})
                elif msg.get("type") == "resume":
                    from yukti.data.state import set_halt
                    await set_halt(False)
                    await websocket.send_json({"type": "ack", "halted": False})
                elif msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            manager.disconnect(websocket)
            log.info("WS client disconnected (remaining=%d)", len(manager._active))

    # ── Serve React SPA ────────────────────────────────────────────────────────
    # The webapp is built with `npm run build` inside webapp/ which outputs
    # to yukti/api/static. In development, Vite proxies /api/* to FastAPI.
    STATIC_DIR = Path(__file__).parent / "static"
    if STATIC_DIR.exists():
        # Mount assets (JS/CSS chunks) at /assets — Vite outputs here
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            """
            Catch-all route: serve index.html for any path not matched by
            the API routes above. This enables client-side React Router navigation.
            """
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(index)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
