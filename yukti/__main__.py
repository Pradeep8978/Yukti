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
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn

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
    """Bring up the full stack for paper/live/shadow mode."""
    from yukti.data.database import create_all_tables
    from yukti.data.state import set_halt
    from yukti.execution.reconcile import reconcile_positions, recover_from_crash
    from yukti.execution.broker_factory import get_broker
    from yukti.execution.monitor import monitor_positions
    from yukti.scheduler.jobs import build_scheduler, is_trading_day
    from yukti.telegram.bot import get_app as tg_app, alert
    from yukti.api.main import app as fastapi_app
    from yukti.watchdog import watchdog_loop

    # ── 1. Bootstrap ────────────────────────────────────────────
    log.info("Yukti starting — mode=%s", mode.upper())
    await create_all_tables()

    # Initialise the correct broker (live/paper/shadow)
    broker = get_broker()

    # Wire broker into dhan_client module so existing code works
    import yukti.execution.dhan_client as _dc
    _dc.dhan = broker

    # ── 2. CRASH RECOVERY — run before anything else ───────────
    log.info("Running crash recovery scan...")
    recovery_stats = await recover_from_crash()
    if recovery_stats.get("emergency_exit", 0) > 0:
        log.critical("Emergency exits performed during recovery: %d", recovery_stats["emergency_exit"])

    # ── 3. Daily reconciliation (if trading day) ───────────────
    if is_trading_day():
        ok = await reconcile_positions()
        if not ok:
            log.critical("Reconciliation failed — agent starts HALTED")
            await set_halt(True)

    # ── 4. Scheduler ───────────────────────────────────────────
    scheduler = build_scheduler()
    scheduler.start()
    log.info("Scheduler started (IST)")

    # ── 5. Telegram ────────────────────────────────────────────
    try:
        tg = tg_app()
        await tg.initialize()
        await tg.start()
        asyncio.create_task(tg.updater.start_polling())
        await alert(f"🚀 Yukti started in *{mode.upper()}* mode")
        log.info("Telegram bot active")
    except Exception as exc:
        log.warning("Telegram startup failed: %s", exc)

    # ── 6. FastAPI web portal ──────────────────────────────────
    config = uvicorn.Config(
        fastapi_app, host="0.0.0.0", port=8000, log_level="warning",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    log.info("FastAPI listening on :8000")

    # ── 7. Position monitor ────────────────────────────────────
    asyncio.create_task(monitor_positions(poll_interval=10))
    log.info("Position monitor running (10s poll)")

    # ── 8. Dead man's switch ───────────────────────────────────
    asyncio.create_task(watchdog_loop(
        check_interval     = 60,
        timeout_multiplier = 3,
        auto_halt          = True,
    ))
    log.info("Watchdog running")

    # ── 9. Main signal loop ────────────────────────────────────
    universe = await _load_universe()
    log.info("Universe: %d symbols", len(universe))
    if mode == "paper":
        # For paper mode, run a single scan cycle instead of continuous loop
        await _single_scan_cycle(universe)
    else:
        await _signal_loop(universe, mode)


async def _single_scan_cycle(universe: dict[str, str]) -> None:
    """
    Run a single scan cycle for paper mode testing.
    """
    from yukti.data.state import is_halted, get_performance_state
    from yukti.execution.dhan_client import dhan
    from yukti.signals.context import build_context
    from yukti.signals.indicators import compute
    from yukti.agents.arjun import arjun
    from yukti.risk import calculate_levels, calculate_position, run_gates
    from yukti.execution.order_sm import open_trade
    from yukti.metrics import record_skip, record_trade_opened
    from yukti.telegram.bot import alert_trade_opened

    log.info("Starting single scan cycle for paper mode")

    # Get Nifty context
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        nifty_raw = await dhan.get_candles("13", settings.candle_interval, start, today)
        if not nifty_raw or len(nifty_raw) < 20:
            log.warning("No Nifty data, using defaults")
            nifty_chg = 0.0
            nifty_trend = "SIDEWAYS"
        else:
            nifty_df = pd.DataFrame(
                nifty_raw,
                columns=["time", "open", "high", "low", "close", "volume"],
            ).astype({"close": float, "open": float})
            nifty_chg = float(
                (nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-2])
                / nifty_df["close"].iloc[-2] * 100
            )
            nifty_trend = "UP" if nifty_df["close"].iloc[-1] > nifty_df["close"].iloc[-10] else "DOWN"
    except Exception as exc:
        log.warning("Nifty fetch failed: %s, using defaults", exc)
        nifty_chg = 0.0
        nifty_trend = "SIDEWAYS"

    perf = await get_performance_state()

    # Scan all symbols sequentially for simplicity
    for symbol, security_id in universe.items():
        if await is_halted():
            log.info("Halted, stopping scan")
            break

        log.info(f"Scanning {symbol} ({security_id})")
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            raw = await dhan.get_candles(security_id, settings.candle_interval, start, today)
            if not raw or len(raw) < 60:
                log.warning(f"No sufficient candle data for {symbol}")
                continue

            df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
            df = df.astype({c: float for c in ["open","high","low","close","volume"]})
            snap = compute(df)
            log.debug(f"Computed indicators for {symbol}")

            # Skip pattern pre-filter for now
            past_journal = await retrieve_similar(symbol, "unknown", "LONG")
            log.debug(f"Retrieved past journal for {symbol}")

            context = build_context(
                symbol, snap, nifty_chg, nifty_trend,
                "No breaking news", perf, past_journal,
            )

            decision = await arjun.safe_decide(context)
            log.info(f"AI decision for {symbol}: {decision.action} (conviction {decision.conviction})")

            if decision.action == "SKIP":
                record_skip(decision.skip_reason or "claude_skip")
                continue

            # Fill missing levels
            if not decision.stop_loss or not decision.target_1:
                levels = calculate_levels(
                    decision.direction or "LONG",
                    decision.entry_price or snap.close,
                    snap.atr, snap.nearest_swing_low, snap.nearest_swing_high,
                )
                decision.stop_loss = decision.stop_loss or levels.stop_loss
                decision.target_1 = decision.target_1 or levels.target_1
                decision.target_2 = decision.target_2 or levels.target_2
                decision.risk_reward = decision.risk_reward or levels.risk_reward
                log.debug(f"Fallback levels calculated for {symbol}")

            position = calculate_position(
                decision.entry_price or snap.close,
                decision.stop_loss,
                decision.direction or "LONG",
                decision.conviction,
            )
            log.debug(f"Position calculated for {symbol}: qty {position.quantity}")

            gate = await run_gates(
                symbol, decision.direction or "LONG",
                decision.risk_reward or 0.0, position,
            )
            if not gate.passed:
                record_skip(gate.reason or "gate_blocked")
                log.info(f"Risk gate failed for {symbol}: {gate.reason}")
                continue

            log.info(f"Opening trade for {symbol}")
            pos = await open_trade(symbol, security_id, decision, position)
            if pos:
                record_trade_opened(
                    decision.direction or "LONG",
                    decision.setup_type or "unknown",
                )
                await alert_trade_opened(pos)
                # Basic journaling
                try:
                    from yukti.agents.journal import write_journal_entry
                    await write_journal_entry(
                        trade={
                            "symbol": symbol,
                            "direction": decision.direction,
                            "setup_type": decision.setup_type,
                            "entry_price": decision.entry_price,
                            "stop_loss": decision.stop_loss,
                            "target_1": decision.target_1,
                            "exit_price": None,
                            "exit_reason": None,
                            "pnl_pct": None,
                            "conviction": decision.conviction,
                            "reasoning": decision.reasoning,
                        },
                        original_reasoning=decision.reasoning,
                    )
                    log.info(f"Journaled trade for {symbol}")
                except Exception as exc:
                    log.warning("Basic journaling failed: %s", exc)

        except Exception as exc:
            log.error("Scan error %s: %s", symbol, exc, exc_info=True)

    log.info("Single scan cycle complete")


async def _signal_loop(universe: dict[str, str], mode: str) -> None:
    """
    Per-candle signal scanning loop with:
      - Backpressure: skip cycle if previous still running
      - Concurrency cap: max 5 symbols scanned in parallel
      - Heartbeat: updates watchdog on every successful cycle
    """
    from yukti.agents.arjun import arjun
    from yukti.agents.memory import retrieve_similar
    from yukti.data.state import is_halted, get_performance_state
    from yukti.execution.dhan_client import dhan
    from yukti.execution.order_sm import open_trade
    from yukti.metrics import (
        signals_scanned, record_skip, record_trade_opened,
        signal_loop_last_run,
    )
    from yukti.risk import calculate_levels, calculate_position, run_gates
    from yukti.scheduler.jobs import is_trading_day, is_trading_hours
    from yukti.signals.context import build_context
    from yukti.signals.indicators import compute
    from yukti.signals.patterns import best_pattern
    from yukti.telegram.bot import alert_trade_opened
    from yukti.watchdog import heartbeat

    import pandas as pd

    interval_secs   = int(settings.candle_interval) * 60
    NIFTY_ID        = "13"
    MAX_CONCURRENT  = 5   # Semaphore to avoid blasting broker + AI concurrently
    sem             = asyncio.Semaphore(MAX_CONCURRENT)

    log.info("Signal loop started — interval=%sm mode=%s concurrency=%d",
             settings.candle_interval, mode, MAX_CONCURRENT)

    cycle_in_progress = False

    while True:
        cycle_start = asyncio.get_event_loop().time()

        # ── Backpressure: skip if previous cycle hasn't finished ──
        if cycle_in_progress:
            log.warning("Previous cycle still running — skipping this candle")
            await asyncio.sleep(30)
            continue

        # ── Halt / hours check ─────────────────────────────────
        if await is_halted():
            await asyncio.sleep(30)
            continue

        if not is_trading_day() or not is_trading_hours():
            heartbeat()   # still alive, just off-hours
            await asyncio.sleep(30)
            continue

        cycle_in_progress = True
        try:
            # ── Nifty context ───────────────────────────────────
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                nifty_raw = await dhan.get_candles(NIFTY_ID, settings.candle_interval, start, today)
                if not nifty_raw or len(nifty_raw) < 20:
                    await asyncio.sleep(60)
                    continue
                nifty_df = pd.DataFrame(
                    nifty_raw,
                    columns=["time", "open", "high", "low", "close", "volume"],
                ).astype({"close": float, "open": float})
                nifty_chg   = float(
                    (nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-2])
                    / nifty_df["close"].iloc[-2] * 100
                )
                nifty_trend = "UP" if nifty_df["close"].iloc[-1] > nifty_df["close"].iloc[-10] else "DOWN"
            except Exception as exc:
                log.warning("Nifty fetch failed: %s", exc)
                await asyncio.sleep(60)
                continue

            perf = await get_performance_state()

            # ── Scan all symbols concurrently with bounded parallelism ──
            tasks = [
                _scan_symbol(symbol, security_id, nifty_chg, nifty_trend, perf, sem)
                for symbol, security_id in universe.items()
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            # ── Heartbeat after successful cycle ──
            heartbeat()
            signal_loop_last_run.set(asyncio.get_event_loop().time())

        finally:
            cycle_in_progress = False

        elapsed = asyncio.get_event_loop().time() - cycle_start
        log.info("Cycle complete in %.1fs, sleeping %.1fs", elapsed, max(5, interval_secs - elapsed))
        await asyncio.sleep(max(5, interval_secs - elapsed))


async def _scan_symbol(
    symbol:      str,
    security_id: str,
    nifty_chg:   float,
    nifty_trend: str,
    perf:        dict,
    sem:         asyncio.Semaphore,
) -> None:
    """Scan one symbol — runs concurrently under semaphore."""
    from yukti.agents.arjun import arjun
    from yukti.agents.memory import retrieve_similar
    from yukti.execution.dhan_client import dhan
    from yukti.execution.order_sm import open_trade
    from yukti.metrics import signals_scanned, record_skip, record_trade_opened
    from yukti.risk import calculate_levels, calculate_position, run_gates
    from yukti.signals.context import build_context
    from yukti.signals.indicators import compute
    from yukti.signals.patterns import best_pattern
    from yukti.telegram.bot import alert_trade_opened

    import pandas as pd

    async with sem:
        signals_scanned.inc()
        log.info(f"Scanning {symbol} ({security_id})")
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            raw = await dhan.get_candles(security_id, settings.candle_interval, start, today)
            if not raw or len(raw) < 60:
                log.warning(f"No sufficient candle data for {symbol}")
                return

            df   = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
            df   = df.astype({c: float for c in ["open","high","low","close","volume"]})
            snap = compute(df)
            log.debug(f"Computed indicators for {symbol}")

            # Cheap pre-filter
            # if best_pattern(snap) is None:
            #     record_skip("no_pattern")
            #     return

            past_journal = await retrieve_similar(symbol, "unknown", "LONG")
            log.debug(f"Retrieved past journal for {symbol}")

            context = build_context(
                symbol, snap, nifty_chg, nifty_trend,
                "No breaking news", perf, past_journal,
            )

            decision = await arjun.safe_decide(context)
            log.info(f"AI decision for {symbol}: {decision.action} (conviction {decision.conviction})")

            if decision.action == "SKIP":
                record_skip(decision.skip_reason or "claude_skip")
                return

            # Fill missing levels
            if not decision.stop_loss or not decision.target_1:
                levels = calculate_levels(
                    decision.direction or "LONG",
                    decision.entry_price or snap.close,
                    snap.atr, snap.nearest_swing_low, snap.nearest_swing_high,
                )
                decision.stop_loss   = decision.stop_loss   or levels.stop_loss
                decision.target_1    = decision.target_1    or levels.target_1
                decision.target_2    = decision.target_2    or levels.target_2
                decision.risk_reward = decision.risk_reward or levels.risk_reward
                log.debug(f"Fallback levels calculated for {symbol}")

            position = calculate_position(
                decision.entry_price or snap.close,
                decision.stop_loss,
                decision.direction or "LONG",
                decision.conviction,
            )
            log.debug(f"Position calculated for {symbol}: qty {position.quantity}")

            gate = await run_gates(
                symbol, decision.direction or "LONG",
                decision.risk_reward or 0.0, position,
            )
            if not gate.passed:
                record_skip(gate.reason or "gate_blocked")
                log.info(f"Risk gate failed for {symbol}: {gate.reason}")
                return

            log.info(f"Opening trade for {symbol}")
            pos = await open_trade(symbol, security_id, decision, position)
            if pos:
                record_trade_opened(
                    decision.direction or "LONG",
                    decision.setup_type or "unknown",
                )
                await alert_trade_opened(pos)
                # Basic journaling
                try:
                    from yukti.agents.journal import write_journal_entry
                    await write_journal_entry(
                        trade={
                            "symbol": symbol,
                            "direction": decision.direction,
                            "setup_type": decision.setup_type,
                            "entry_price": decision.entry_price,
                            "stop_loss": decision.stop_loss,
                            "target_1": decision.target_1,
                            "exit_price": None,
                            "exit_reason": None,
                            "pnl_pct": None,
                            "conviction": decision.conviction,
                            "reasoning": decision.reasoning,
                        },
                        original_reasoning=decision.reasoning,
                    )
                    log.info(f"Journaled trade for {symbol}")
                except Exception as exc:
                    log.warning("Basic journaling failed: %s", exc)

        except Exception as exc:
            log.error("Scan error %s: %s", symbol, exc, exc_info=True)


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
