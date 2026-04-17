"""
yukti/services/control_plane_service.py
Handles control plane: API server, Telegram bot, watchdog.
"""
from __future__ import annotations

import asyncio
import logging
import uvicorn

from yukti.api.main import create_app
from yukti.telegram.bot import get_app as tg_app, alert
from yukti.watchdog import watchdog_loop

log = logging.getLogger(__name__)


class ControlPlaneService:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.api_server = None
        self.tg_task = None
        self.watchdog_task = None

    async def start(self) -> None:
        """Start all control plane services."""
        log.info("ControlPlaneService: starting for mode=%s", self.mode)

        # Start API server
        app = create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
        self.api_server = uvicorn.Server(config)
        asyncio.create_task(self.api_server.serve())
        log.info("ControlPlaneService: API server started on :8000")

        # Start Telegram bot
        try:
            tg = tg_app()
            await tg.initialize()
            self.tg_task = asyncio.create_task(tg.updater.start_polling())
            await alert(f"🚀 Yukti started in *{self.mode.upper()}* mode")
            log.info("ControlPlaneService: Telegram bot active")
        except Exception as exc:
            log.warning("ControlPlaneService: Telegram startup failed: %s", exc)

        # Start watchdog
        self.watchdog_task = asyncio.create_task(watchdog_loop(check_interval=60, timeout_multiplier=3, auto_halt=True))
        log.info("ControlPlaneService: watchdog running")

    async def stop(self) -> None:
        """Stop all services."""
        log.info("ControlPlaneService: stopping")

        if self.watchdog_task:
            self.watchdog_task.cancel()
        if self.tg_task:
            self.tg_task.cancel()
        if self.api_server:
            await self.api_server.shutdown()

        log.info("ControlPlaneService: stopped")