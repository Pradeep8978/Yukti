"""
yukti/__main__.py
Entry point for the Yukti trading agent.

Modes:
    paper    — full agent logic, PaperBroker for simulated fills
    live     — real DhanHQ orders (real money)
    shadow   — live DhanHQ market data, orders logged but never placed
    backtest — replay historical candles, no live feed

Usage:
    uv run python -m yukti                    # uses MODE from .env (default: paper)
    uv run python -m yukti --mode shadow      # override to shadow
    uv run python -m yukti --mode backtest --bt-start 2024-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from yukti.config import settings

log = logging.getLogger("yukti.main")


def _configure_logging() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt = "%H:%M:%S",
        handlers = [logging.StreamHandler(sys.stdout)],
    )
    for noisy in ("httpx", "httpcore", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _load_universe() -> dict[str, str]:
    try:
        import redis.asyncio as aioredis
        r   = await aioredis.from_url(settings.redis_url, decode_responses=True)
        raw = await r.get("yukti:universe")
        await r.aclose()
        if raw:
            universe_list = json.loads(raw)
            return {u["symbol"]: u["security_id"] for u in universe_list}
    except Exception:
        pass

    fallback = Path("universe.json")
    if fallback.exists():
        return json.loads(fallback.read_text())

    log.warning("No universe found — using built-in 5-symbol universe")
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

    # Bootstrap
    bootstrap = BootstrapService()
    await bootstrap.bootstrap(mode)

    # Load universe
    universe = await _load_universe()
    log.info("Universe: %d symbols", len(universe))

    # Market scan service
    scanner = MarketScanService(universe)

    if mode == "paper":
        # Single scan for paper mode
        await scanner.run_single_scan()
    else:
        # Continuous scan for live/shadow
        scan_task = asyncio.create_task(scanner.run_continuous_scan())

        # Control plane
        control = ControlPlaneService(mode)
        await control.start()

        # Wait for scan to finish (it runs forever)
        await scan_task


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
        log.info("Shutdown requested — stopping gracefully")
    finally:
        loop.close()


if __name__ == "__main__":
    main()