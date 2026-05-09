"""
yukti/services/control_plane_service.py
Handles control plane: API server, Telegram bot, watchdog.
"""
from __future__ import annotations

import asyncio
import logging
import uvicorn

from yukti.api.main import create_app
from yukti.telegram.bot import get_app as tg_app, alert, alert_agent_started, alert_agent_shutdown
from yukti.watchdog import watchdog_loop
from yukti.scheduler.jobs import build_scheduler

log = logging.getLogger(__name__)


class ControlPlaneService:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.api_server = None
        self.tg = None
        self.watchdog_task = None
        self.scheduler = None

    async def start(self) -> None:
        """Start all control plane services."""
        from yukti.config import settings

        # Log all critical trading parameters at every startup for audit trail
        log.info(
            "Trading config: mode=%s account=₹%.0f risk=%.1f%% leverage=%.0fx "
            "max_pos=%d daily_loss=%.1f%% max_loss_cap=%.1f%%",
            settings.mode,
            settings.account_value,
            settings.risk_pct * 100,
            settings.intraday_leverage,
            settings.max_open_positions,
            settings.daily_loss_limit_pct * 100,
            settings.max_loss_cap_pct * 100,
        )

        # Validate live-mode prerequisites — fail fast rather than fail mid-trade
        if settings.mode == "live":
            if not settings.dhan_client_id:
                raise RuntimeError("LIVE MODE requires DHAN_CLIENT_ID to be set in .env")
            if not settings.dhan_access_token:
                raise RuntimeError("LIVE MODE requires DHAN_ACCESS_TOKEN to be set in .env")
            if not settings.control_api_key:
                log.warning("LIVE MODE: CONTROL_API_KEY is empty — /control/* endpoints are unprotected")
            if settings.risk_pct > settings.max_loss_cap_pct * 0.6:
                log.warning(
                    "CONFIG WARNING: risk_pct=%.1f%% is close to max_loss_cap=%.1f%% — "
                    "high-conviction trades (1.5× multiplier) may be blocked by gate 7",
                    settings.risk_pct * 100,
                    settings.max_loss_cap_pct * 100,
                )

        log.info("ControlPlaneService: starting for mode=%s", self.mode)

        # Start API server
        app = create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
        self.api_server = uvicorn.Server(config)
        asyncio.create_task(self.api_server.serve())
        log.info("ControlPlaneService: API server started on :8000")

        # Start Telegram bot
        # PTB v20+ lifecycle: initialize() → start() → updater.start_polling().
        # Without start(), the dispatcher never consumes updates from the queue,
        # so /status (and every other command) silently goes unanswered.
        try:
            tg = tg_app()
            await tg.initialize()
            await tg.start()
            await tg.updater.start_polling()
            self.tg = tg
            await alert_agent_started(self.mode)
            log.info("ControlPlaneService: Telegram bot active")
        except Exception as exc:
            log.warning("ControlPlaneService: Telegram startup failed: %s", exc)

        # Start watchdog
        self.watchdog_task = asyncio.create_task(watchdog_loop(check_interval=60, timeout_multiplier=2, auto_halt=True))
        log.info("ControlPlaneService: watchdog running")

        # Start scheduler (registered cron jobs)
        try:
            self.scheduler = build_scheduler()
            self.scheduler.start()
            log.info("ControlPlaneService: scheduler started")
            # Run a quick DhanHQ API startup check in background
            try:
                from yukti.scheduler.jobs import job_test_dhan_api
                asyncio.create_task(job_test_dhan_api())
                log.info("ControlPlaneService: scheduled DhanHQ startup API check")
            except Exception as exc:
                log.warning("ControlPlaneService: failed to schedule DhanHQ startup check: %s", exc)
        except Exception as exc:
            log.warning("ControlPlaneService: Scheduler startup failed: %s", exc)

        # Start live WebSocket feed for real-time SL/target detection
        try:
            from yukti.execution.live_feed import get_feed_manager
            from yukti.execution.monitor import _on_tick
            feed = get_feed_manager()
            await feed.start(_on_tick)
            log.info("ControlPlaneService: live feed started")
        except Exception as exc:
            log.warning("ControlPlaneService: live feed startup failed (non-fatal): %s", exc)

    async def stop(self, reason: str = "") -> None:
        """Stop all services."""
        log.info("ControlPlaneService: stopping")
        try:
            await alert_agent_shutdown(reason)
        except Exception:
            pass

        if self.watchdog_task:
            self.watchdog_task.cancel()
        if self.tg is not None:
            try:
                if self.tg.updater and self.tg.updater.running:
                    await self.tg.updater.stop()
                await self.tg.stop()
                await self.tg.shutdown()
            except Exception as exc:
                log.warning("ControlPlaneService: Telegram shutdown failed: %s", exc)
        if self.scheduler:
            try:
                self.scheduler.shutdown(wait=False)
                log.info("ControlPlaneService: scheduler stopped")
            except Exception as exc:
                log.warning("ControlPlaneService: scheduler shutdown failed: %s", exc)
        if self.api_server:
            await self.api_server.shutdown()

        log.info("ControlPlaneService: stopped")