"""
yukti/__main__.py
Entry point for the Yukti trading agent.

Modes:
    paper    — full agent logic, PaperBroker for simulated fills
    live     — real DhanHQ orders (real money)
    shadow   — live DhanHQ market data, orders logged but never placed
    backtest — replay historical candles, no live feed

Usage:
    uv run python -m yukti                    # uses MODE from .env (default: live)
    uv run python -m yukti --mode shadow      # override to shadow
    uv run python -m yukti --mode backtest --bt-start 2024-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from yukti.config import settings

log = logging.getLogger("yukti.main")

# Module-level references so graceful shutdown can reach the control plane and scan task
_control_plane: "ControlPlaneService | None" = None
_scan_task: "asyncio.Task | None" = None
_shutdown_event: "asyncio.Event | None" = None


def _configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    yukti_level = getattr(logging, level_name, logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(
        "%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    ))

    # Daily rotation, written to bind-mounted /app/logs/ so logs survive
    # container recreates (token-renewal cron at 08:00 / 18:00 IST).
    log_dir = Path(os.environ.get("LOG_DIR", "/app/logs"))
    handlers: list[logging.Handler] = [stream]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_h = TimedRotatingFileHandler(
            log_dir / "yukti.log",
            when="midnight",
            backupCount=int(os.environ.get("LOG_BACKUP_DAYS", "14")),
            utc=False,
        )
        file_h.suffix = "%Y-%m-%d"
        file_h.setFormatter(logging.Formatter(
            "%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(file_h)
    except Exception as exc:
        print(f"warn: file logging disabled ({exc})", file=sys.stderr)

    # Root logger at INFO so external libs don't flood at DEBUG; yukti.* gets
    # the configured level so our own modules can be DEBUG-verbose.
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("yukti").setLevel(yukti_level)
    for noisy in ("httpx", "httpcore", "anthropic", "asyncio",
                  "sqlalchemy", "sqlalchemy.engine", "websockets",
                  "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("Logging configured: yukti.*=%s root=INFO file=%s/yukti.log",
             level_name, log_dir)


async def _load_universe() -> dict[str, str]:
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await r.get("yukti:universe")
            entries = json.loads(raw) if raw else []
            if not entries:
                # universe key missing or empty list — fall back to candidate pool
                if raw is not None:
                    log.warning("Universe key is empty list — falling back to yukti:candidate_pool")
                raw_pool = await r.get("yukti:candidate_pool")
                if raw_pool:
                    log.info("Universe key empty — falling back to yukti:candidate_pool")
                    entries = json.loads(raw_pool)
        finally:
            await r.aclose()
        if entries:
            return {u["symbol"]: u["security_id"] for u in entries}
    except Exception as exc:
        log.warning("Universe load from Redis failed: %s — falling back", exc)

    fallback = Path("universe.json")
    if fallback.exists():
        return json.loads(fallback.read_text())

    if settings.mode in ("live", "shadow"):
        # Self-heal: Redis was wiped AND the baked universe.json is missing.
        # Rather than crash-loop (which never lets the scheduler rebuild the
        # universe), run the scanner's fallback chain inline — it returns a
        # Nifty-50 baseline and re-seeds yukti:universe in Redis.
        log.warning(
            "No universe in Redis or universe.json — self-healing via "
            "UniverseScannerService.run_with_fallback() (mode=%s)", settings.mode
        )
        try:
            from yukti.services.universe_scanner_service import UniverseScannerService
            entries = await UniverseScannerService().run_with_fallback(is_refresh=False)
            if entries:
                log.info("Self-heal succeeded: %d symbols seeded", len(entries))
                return {u["symbol"]: u["security_id"] for u in entries}
        except Exception as exc:
            log.error("Self-heal universe build failed: %s", exc)
        raise RuntimeError(
            "No universe found and self-heal failed — refusing to start in "
            f"mode={settings.mode} with the built-in 5-symbol fallback. Run "
            "scripts/universe_loader.py first."
        )

    log.warning("No universe found — using built-in 5-symbol universe (mode=%s)", settings.mode)
    return {
        "RELIANCE":  "1333",
        "HDFCBANK":  "1232",
        "INFY":      "1594",
        "TCS":       "11536",
        "ICICIBANK": "4963",
    }


async def _run_paper_or_live(mode: str) -> None:
    """Run Yukti using services."""
    from yukti.services.bootstrap_service import BootstrapService
    from yukti.services.market_scan_service import MarketScanService
    from yukti.services.control_plane_service import ControlPlaneService

    # Refuse to start sensitive modes without a configured control API key.
    if mode in ("live", "shadow") and not settings.control_api_key:
        raise RuntimeError(
            f"control_api_key is required in mode={mode} — kill-switch / control "
            "endpoints would be unauthenticated. Set CONTROL_API_KEY in the environment."
        )

    # Bootstrap
    bootstrap = BootstrapService()
    await bootstrap.bootstrap(mode)

    # Load universe
    universe = await _load_universe()
    log.info("Universe: %d symbols", len(universe))

    # Market scan service
    scanner = MarketScanService(universe)

    global _control_plane, _scan_task, _shutdown_event
    _shutdown_event = asyncio.Event()

    # Install POSIX signal handlers so SIGTERM (docker stop) shuts us down too.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_event.set)
        except NotImplementedError:
            # Windows or restricted env — fall back to default handling.
            pass

    _scan_task = asyncio.create_task(scanner.run_continuous_scan(), name="scan")

    # Control plane
    _control_plane = ControlPlaneService(mode)
    await _control_plane.start()

    shutdown_task = asyncio.create_task(_shutdown_event.wait(), name="shutdown_wait")
    done, _pending = await asyncio.wait(
        {_scan_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_task in done:
        log.info("Shutdown signal received — cancelling scan loop")
        _scan_task.cancel()
        try:
            await _scan_task
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            if not isinstance(exc, asyncio.CancelledError):
                log.warning("Scan task raised during shutdown: %s", exc)
    else:
        shutdown_task.cancel()

    if _control_plane is not None:
        await _control_plane.stop()


async def _run_backtest(start: str, end: str, sample_rate: float) -> None:
    from yukti.data.database import create_all_tables
    from yukti.backtest import BacktestEngine
    import pandas as pd

    await create_all_tables()
    universe = await _load_universe()

    from sqlalchemy import select
    from yukti.data.database import get_db
    from yukti.data.models import Candle

    candles: dict[str, pd.DataFrame] = {}
    async with get_db() as db:
        for symbol in universe:
            rows = (await db.execute(
                select(Candle)
                .where(Candle.symbol == symbol)
                .order_by(Candle.time)
            )).scalars().all()
            if rows:
                df = pd.DataFrame(
                    [(r.time, r.open, r.high, r.low, r.close, r.volume) for r in rows],
                    columns=["time","open","high","low","close","volume"],
                ).set_index("time")
                candles[symbol] = df.astype(float)

    if not candles:
        log.error("No candle data found — populate the candles table first")
        return

    nifty_df = candles.get("NIFTY", next(iter(candles.values())))
    engine   = BacktestEngine(
        candles, nifty_df,
        account_value      = settings.account_value,
        claude_sample_rate = sample_rate,
    )
    report = await engine.run()
    report.print_summary()
    report.to_csv("backtest_trades.csv")


def main() -> None:
    _configure_logging()

    # ── Fix the default asyncio thread pool size ──────────────
    # Default is min(32, cpu_count+4). DhanHQ SDK calls all go through
    # run_in_executor, so we need headroom.
    loop = asyncio.new_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=20))
    asyncio.set_event_loop(loop)

    parser = argparse.ArgumentParser(description="Yukti trading agent")
    parser.add_argument("--mode", choices=["paper","live","shadow","backtest"], default=settings.mode)
    parser.add_argument("--bt-start",  default="2024-01-01")
    parser.add_argument("--bt-end",    default="2024-12-31")
    parser.add_argument("--bt-sample", type=float, default=0.3)
    args = parser.parse_args()

    # Override settings.mode if flag provided
    if args.mode != settings.mode:
        import os
        os.environ["MODE"] = args.mode
        settings.mode = args.mode  # type: ignore

    log.info("=" * 60)
    log.info("  YUKTI (युक्ति) — Autonomous NSE Trading Agent")
    log.info("  Mode:           %s", args.mode.upper())
    log.info("  AI provider:    %s", settings.ai_provider.upper())
    log.info("  Account:        ₹%s", f"{settings.account_value:,.0f}")
    log.info("  Risk per trade: %.1f%%", settings.risk_pct * 100)
    log.info("  Candle:         %s min", settings.candle_interval)
    log.info("=" * 60)

    try:
        if args.mode == "backtest":
            loop.run_until_complete(_run_backtest(args.bt_start, args.bt_end, args.bt_sample))
        else:
            loop.run_until_complete(_run_paper_or_live(args.mode))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — stopping gracefully")
        if _control_plane is not None:
            loop.run_until_complete(_control_plane.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()